"""Public-source parsing, quota wiring and free-credit boundaries."""
from datetime import datetime, timezone
import unittest
from unittest.mock import Mock

import requests

from automation.discovery import (DiscoveryClient, PROFILE_POSTS_ACTOR, discover_targets,
                                  normalize_profile_post, post_urn, profile_handle,
                                  read_budget_available)
from lib.apify_client import ApifyError


ID = '7000000000000000000'
URN = 'urn:li:activity:' + ID
URL = 'https://www.linkedin.com/feed/update/' + URN + '/'
TEXT = 'Good design starts with a clear product decision.'


class DiscoveryTests(unittest.TestCase):
    def client(self, payload=None):
        client = DiscoveryClient(token='test-placeholder')
        client._run_sync = Mock(return_value=payload or [])
        return client

    def test_supported_post_urls_strip_tracking_and_preserve_identity(self):
        for value, urn in (
            (URL + '?trk=feed#comment', URN),
            ('https://linkedin.com/posts/design_activity-' + ID + '-abcd?x=1', URN),
            ('https://www.linkedin.com/feed/update/urn%3Ali%3AugcPost%3A' + ID, 'urn:li:ugcPost:' + ID),
            ('https://www.linkedin.com/posts/design_share-' + ID + '-abcd/', 'urn:li:share:' + ID),
            (URN, URN),
        ):
            with self.subTest(value=value):
                self.assertEqual(post_urn(value), urn)

    def test_unsafe_or_non_post_urls_never_become_targets(self):
        for value in (
            'https://www.linkedin.com.attacker.test/feed/update/' + URN,
            'https://www.linkedin.com@attacker.test/feed/update/' + URN,
            'https://attacker.test/posts/person_activity-' + ID,
            'http://linkedin.com/feed/update/' + URN,
            'https://user:password@linkedin.com/feed/update/' + URN,
            'https://linkedin.com:8443/feed/update/' + URN,
            'https://linkedin.com/in/person/?redirect=' + URN,
            'https://linkedin.com/foo/' + URN,
            'https://linkedin.com/feed/update/' + URN + '/extra',
            'urn:li:comment:(' + URN + ',42)',
            'urn:li:activity:123',
            'urn:li:activity:' + '7' * 26,
            'urn:li:activity:' + '\u0667' * 19,
            'https://linkedin.com/posts/person_activity-' + '7' * 26 + '-abcd',
            'https://linkedin.com:bad/feed/update/' + URN,
            None,
        ):
            with self.subTest(value=value):
                self.assertIsNone(post_urn(value))

    def test_profile_handles_use_exact_host_and_profile_path(self):
        self.assertEqual(profile_handle('SomeDesigner'), 'somedesigner')
        self.assertEqual(profile_handle('https://www.LINKEDIN.com/in/SomeDesigner/?trk=feed'), 'somedesigner')
        for value in ('https://linkedin.com.attacker.test/in/designer/',
                      'https://user@linkedin.com/in/designer/',
                      'https://linkedin.com/company/designer/', 'designer/name', None):
            with self.subTest(value=value):
                self.assertIsNone(profile_handle(value))

    def test_nested_actor_normalization_and_datetime(self):
        row = {'post': {'text': TEXT, 'url': URL + '?trk=feed',
                        'urn': {'activity_urn': ID}, 'created_at': '2026-10-09T03:30:00Z'},
               'author': {'name': 'Designer', 'profile_url': 'https://linkedin.com/in/somedesigner/'}}
        result = normalize_profile_post(row, 'source')
        self.assertEqual(result['url'], URL)
        self.assertEqual(result['urn'], URN)
        self.assertEqual(result['text'], TEXT)
        self.assertEqual(result['authorProfileUrl'], 'https://www.linkedin.com/in/somedesigner/')
        self.assertEqual(result['postedAtISO'], '2026-10-09T03:30:00+00:00')
        self.assertEqual(result['origin'], 'profile-discovery')
        self.assertEqual(result['sourceProfile'], 'https://www.linkedin.com/in/source/')
        self.assertNotIn('_raw', result)

    def test_explicit_urn_takes_priority_over_different_activity_url(self):
        actual = 'urn:li:ugcPost:7000000000000000001'
        row = {'urn': actual, 'post_url': URL, 'text': TEXT}
        result = normalize_profile_post(row, 'designer')
        self.assertEqual(result['urn'], actual)
        self.assertEqual(result['url'], URL)
        self.assertEqual(result['shareUrn'], actual)

    def test_explicit_share_identity_keeps_different_activity_url_for_reads(self):
        actual = 'urn:li:share:7000000000000000002'
        for share_fields in ({'urn': {'activity_urn': ID, 'share_urn': '7000000000000000002'}},
                             {'share_id': '7000000000000000002'}, {'share_urn': actual},
                             {'full_urn': actual}):
            with self.subTest(share_fields=share_fields):
                row = {'url': URL, 'text': TEXT, **share_fields}
                result = normalize_profile_post(row, 'designer')
                self.assertEqual(result['urn'], actual)
                self.assertEqual(result['shareUrn'], actual)
                self.assertEqual(result['url'], URL)

    def test_activity_metadata_cannot_be_relabelled_as_share(self):
        result = normalize_profile_post({'url': URL, 'text': TEXT, 'share_urn': URN}, 'designer')
        self.assertEqual(result['urn'], URN)
        self.assertIsNone(result['shareUrn'])

    def test_flat_actor_variants_preserve_timestamp(self):
        stamp = int(datetime(2026, 10, 9, 3, 30, tzinfo=timezone.utc).timestamp() * 1000)
        for field in ('post_url', 'url', 'postUrl', 'linkedinUrl'):
            with self.subTest(field=field):
                result = normalize_profile_post({field: URL, 'text': TEXT, 'posted_at': {'timestamp': stamp}}, 'source')
                self.assertEqual(result['postedAtISO'], '2026-10-09T03:30:00+00:00')
                self.assertEqual(result['url'], URL)

    def test_verified_actor_shape_prefers_full_urn_and_uses_timestamp_without_zone_date(self):
        # Sanitized shape verified against a successful profile-post actor
        # response. Its activity and canonical UGC identifiers differ.
        actual = 'urn:li:ugcPost:7000000000000000001'
        row = {'urn': {'activity_urn': ID, 'share_urn': None, 'ugcPost_urn': '7000000000000000001'},
               'full_urn': actual, 'url': URL, 'text': TEXT, 'post_type': 'regular',
               'posted_at': {'date': '2026-10-09 03:30:00', 'relative': '2 hours ago',
                             'timestamp': 1791516600000},
               'author': {'first_name': 'Product', 'last_name': 'Designer', 'username': 'designer',
                          'headline': 'Co-Founder', 'profile_url': 'https://linkedin.com/in/designer/'},
               'stats': {'total_reactions': 10}, 'pagination_token': 'ignored'}
        result = normalize_profile_post(row, 'designer')
        self.assertEqual(result['urn'], actual)
        self.assertEqual(result['shareUrn'], actual)
        self.assertEqual(result['url'], URL)
        self.assertEqual(result['authorName'], 'Product Designer')
        self.assertEqual(result['authorHeadline'], 'Co-Founder')
        self.assertEqual(result['postedAtISO'], '2026-10-09T03:30:00+00:00')

    def test_diagnostics_and_unavailable_posts_are_skipped(self):
        for row in ({'profile_input': 'source', 'message': 'Wrong input or no posts found or unknown error'},
                    {'summary': {'posts': 0}}, {'post_url': URL},
                    {'post_url': 'https://attacker.test/' + URN, 'text': TEXT},
                    {'url': URL, 'text': '  '}, None):
            with self.subTest(row=row):
                self.assertIsNone(normalize_profile_post(row, 'source'))

    def test_discovery_uses_one_bounded_schema_valid_request_and_deduplicates(self):
        client = self.client([{'data': {'posts': [
            {'post_url': URL, 'text': TEXT},
            {'url': URL + '?tracking=1', 'text': TEXT},
            {'message': 'not a post'},
        ]}}])
        rows = client.fetch_profile_posts('https://linkedin.com/in/Designer/', limit=5)
        client._run_sync.assert_called_once_with(PROFILE_POSTS_ACTOR,
                                                {'username': 'designer', 'limit': 5, 'page_number': 1})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['url'], URL)

    def test_discovery_never_traverses_reshared_posts_or_replies(self):
        client = self.client([{'text': 'Unavailable post',
                               'reshared_post': {'url': URL, 'text': TEXT},
                               'replies': [{'url': URL, 'text': TEXT}]}])
        self.assertEqual(client.fetch_profile_posts('source'), [])

    def test_discovery_cannot_request_large_or_invalid_profile_reads(self):
        client = self.client()
        for options in ({'username': 'source', 'limit': 100}, {'username': 'source', 'limit': True},
                        {'username': 'https://attacker.test/source'}, {'username': 'source', 'limit': 0}):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    client.fetch_profile_posts(**options)
        client._run_sync.assert_not_called()

    def test_source_rotation_excludes_owner_and_uses_runner_read_callback(self):
        owner_url = 'https://www.linkedin.com/in/owner/'
        policy = {'profile_url': owner_url, 'discovery_profiles': ['owner', 'source', 'SOURCE', 'other']}
        state = {}
        client = Mock()
        other = normalize_profile_post({'url': URL, 'text': TEXT}, 'source')
        own = dict(other, authorProfileUrl=owner_url)
        client.fetch_profile_posts.return_value = [other, own]
        quota = Mock(side_effect=lambda call: call())
        self.assertEqual(discover_targets(policy, client, state, read=quota), [other])
        discover_targets(policy, client, state, read=quota)
        self.assertEqual(client.fetch_profile_posts.call_args_list[0].args, ('source',))
        self.assertEqual(client.fetch_profile_posts.call_args_list[1].args, ('other',))
        self.assertEqual(state['discovery']['profile_cursor'], 2)
        self.assertEqual(quota.call_count, 2)

    def test_no_sources_and_exhausted_daily_quota_do_not_read(self):
        client = Mock()
        self.assertEqual(discover_targets({'profile_url': 'owner'}, client, {}), [])
        state = {}
        quota = Mock(return_value=None)
        self.assertEqual(discover_targets({'profile_url': 'owner', 'discovery_profiles': ['source']},
                                         client, state, read=quota), [])
        client.fetch_profile_posts.assert_not_called()
        self.assertEqual(state['discovery']['last_status'], 'daily-budget-exhausted')

    def test_source_errors_skip_without_provider_details(self):
        state = {}
        client = Mock()
        client.fetch_profile_posts.side_effect = ApifyError('secret provider detail')
        self.assertEqual(discover_targets({'discovery_profiles': ['source']}, client, state), [])
        self.assertNotIn('secret', str(state))
        self.assertEqual(state['discovery']['last_status'], 'source-unavailable')


class ReadBudgetTests(unittest.TestCase):
    def client(self, usage=0, cap=5):
        client = DiscoveryClient(token='test-placeholder')
        response = Mock(status_code=200)
        response.json.return_value = {'data': {'current': {'monthlyUsageUsd': usage},
                                              'limits': {'maxMonthlyUsageUsd': cap}}}
        client._session = Mock()
        client._session.get.return_value = response
        return client

    def test_free_credit_gate_preserves_reserve_and_lower_account_limits(self):
        for usage, cap, available in ((0, 5, True), (3.99, 5, True), (4, 5, False),
                                      (1, 2, True), (2, 2, False), (4, 100, False),
                                      (-1, 5, False), (0, 0, False)):
            with self.subTest(usage=usage, cap=cap):
                self.assertEqual(read_budget_available(self.client(usage, cap)), available)

    def test_budget_check_has_no_actor_call_and_keeps_token_out_of_url(self):
        client = self.client()
        client._run_sync = Mock()
        self.assertTrue(read_budget_available(client))
        client._session.get.assert_called_once_with('https://api.apify.com/v2/users/me/limits',
            headers={'Authorization': 'Bearer test-placeholder'}, timeout=30, allow_redirects=False)
        client._run_sync.assert_not_called()

    def test_unknown_missing_or_invalid_credit_values_fail_closed(self):
        for usage, cap in ((None, 5), (0, None), ('0', 5), (True, 5),
                           (float('nan'), 5), (0, float('inf'))):
            with self.subTest(usage=usage, cap=cap):
                self.assertFalse(read_budget_available(self.client(usage, cap)))
        client = self.client()
        for payload in ({}, {'data': {}}, {'data': {'current': {}}}, [], None):
            with self.subTest(payload=payload):
                client._session.get.return_value.json.return_value = payload
                self.assertFalse(read_budget_available(client))

    def test_failed_or_redirected_budget_check_skips_reading(self):
        client = self.client()
        client._session.get.side_effect = requests.Timeout()
        self.assertFalse(read_budget_available(client))
        client = self.client()
        client._session.get.return_value.status_code = 302
        self.assertFalse(read_budget_available(client))
        client._session.get.return_value.raise_for_status.side_effect = requests.HTTPError('denied')
        self.assertFalse(read_budget_available(client))


if __name__ == '__main__':
    unittest.main()
