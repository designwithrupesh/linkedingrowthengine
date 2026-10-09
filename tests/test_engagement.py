"""Public action pacing, replay protection, and durable scrape reuse."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest
from zoneinfo import ZoneInfo

from automation.engagement import (candidate_targets, canonical_target_urn,
                                   comment_key, mark_deferred, mark_discovered,
                                   next_discovery_profile, public_action_counts,
                                   public_actions_due, should_discover, stash_targets)


ZONE = ZoneInfo('Asia/Calcutta')
POLICY = {'timezone': 'Asia/Calcutta', 'profile_url': 'https://www.linkedin.com/in/owner/',
          'discovery_profiles': ['source', 'other'], 'react_to_target_posts': True,
          'max_public_interactions_per_day': 40, 'max_public_actions_per_session': 4}
ACTIVITY = 'urn:li:activity:7000000000000000000'
SHARE = 'urn:li:ugcPost:7000000000000000001'
URL = 'https://www.linkedin.com/feed/update/' + ACTIVITY + '/'


def at(hour=8, minute=0):
    return datetime(2026, 10, 10, hour, minute, tzinfo=ZONE)


def post(urn=SHARE, url=URL, **extra):
    return {'url': url, 'urn': urn, 'shareUrn': urn if 'activity' not in urn else None,
            'text': 'A specific product-design trade-off to discuss.',
            'authorProfileUrl': 'https://www.linkedin.com/in/source/',
            'postedAtISO': at().isoformat(), **extra}


class PublicPacingTests(unittest.TestCase):
    def test_daily_target_accrues_across_half_hour_slots(self):
        self.assertEqual(public_actions_due(at(8), POLICY, {}), 1)
        self.assertEqual(public_actions_due(at(8, 29), POLICY, {}), 1)
        self.assertEqual(public_actions_due(at(8, 30), POLICY, {}), 2)
        self.assertEqual(public_actions_due(at(9), POLICY, {}), 4)
        self.assertEqual(public_actions_due(at(21, 59), POLICY, {}), 4)

    def test_no_public_writes_outside_window(self):
        for now in (at(7, 59), at(22), at(23, 59)):
            self.assertEqual(public_actions_due(now, POLICY, {}), 0)

    def test_repeated_runs_share_durable_slot_allowance(self):
        state = {'actions': {'comment:a': {'kind': 'comment', 'at': at().isoformat(), 'status': 'sent'}},
                 'days': {'2026-10-10': {'interaction': 1}}}
        self.assertEqual(public_actions_due(at(8, 5), POLICY, state), 0)
        self.assertEqual(public_actions_due(at(8, 30), POLICY, json.loads(json.dumps(state))), 1)

    def test_forty_is_a_hard_cap_even_when_slots_were_missed(self):
        state = {'days': {'2026-10-10': {'public_interaction': 39}}}
        self.assertEqual(public_actions_due(at(21, 30), POLICY, state), 1)
        state['days']['2026-10-10']['public_interaction'] = 40
        self.assertEqual(public_actions_due(at(21, 30), POLICY, state), 0)

    def test_utc_clock_uses_indian_day_and_window(self):
        self.assertEqual(public_actions_due(at().astimezone(timezone.utc), POLICY, {}), 1)
        self.assertEqual(public_actions_due(datetime(2026, 10, 9, 23, 30, tzinfo=timezone.utc), POLICY, {}), 0)

    def test_replies_do_not_reduce_public_quota(self):
        state = {'actions': {
            'comment:a': {'kind': 'comment', 'at': at().isoformat(), 'status': 'sent'},
            'reply:a': {'kind': 'reply', 'at': at().isoformat(), 'status': 'sent'},
        }, 'days': {'2026-10-10': {'interaction': 2}}}
        self.assertEqual(public_action_counts(at(8, 30), POLICY, state),
                         {'reaction': 0, 'comment': 1, 'total': 1})
        self.assertEqual(public_actions_due(at(8, 30), POLICY, state), 1)

    def test_unknown_rejected_and_prepared_attempts_count(self):
        state = {'actions': {
            'reaction:a': {'kind': 'reaction', 'at': at().isoformat(), 'status': 'unknown-needs-reconciliation'},
            'comment:b': {'kind': 'comment', 'at': at().isoformat(), 'status': 'rejected'},
            'comment:c': {'kind': 'comment', 'at': at().isoformat(), 'status': 'prepared'},
        }}
        self.assertEqual(public_action_counts(at(9), POLICY, state),
                         {'reaction': 1, 'comment': 2, 'total': 3})
        self.assertEqual(public_actions_due(at(9), POLICY, state), 1)

    def test_missing_receipts_do_not_release_legacy_quota(self):
        state = {'days': {'2026-10-10': {'interaction': 40}}}
        self.assertEqual(public_actions_due(at(21, 30), POLICY, state), 0)

    def test_missing_counter_does_not_release_recorded_quota(self):
        state = {'actions': {'comment:a': {'kind': 'comment', 'at': at().isoformat()}},
                 'days': {'2026-10-10': {'interaction': 0}}}
        self.assertEqual(public_actions_due(at(), POLICY, state), 0)

    def test_new_public_counter_excludes_replies(self):
        state = {'days': {'2026-10-10': {'interaction': 12, 'public_interaction': 1}}}
        self.assertEqual(public_actions_due(at(8, 30), POLICY, state), 1)

    def test_prior_day_attempts_do_not_consume_today(self):
        state = {'actions': {'comment:a': {'kind': 'comment', 'at': (at() - timedelta(days=1)).isoformat()}},
                 'days': {'2026-10-09': {'interaction': 40}}}
        self.assertEqual(public_actions_due(at(), POLICY, state), 1)

    def test_invalid_policy_and_clock_fail_closed(self):
        for overrides in ({'max_public_interactions_per_day': 80}, {'max_public_actions_per_session': 40},
                          {'max_public_interactions_per_day': True}, {'public_engagement_start_hour': 22},
                          {'public_engagement_slot_minutes': 17}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                public_actions_due(at(), {**POLICY, **overrides}, {})
        with self.assertRaises(ValueError):
            public_actions_due(at().replace(tzinfo=None), POLICY, {})
        with self.assertRaises(ValueError):
            public_actions_due(at(), POLICY, {'days': {'2026-10-10': {'interaction': '40'}}})


class TargetCacheTests(unittest.TestCase):
    def test_new_comment_key_hashes_canonical_identity_not_activity_url(self):
        expected = 'comment:' + hashlib.sha256(SHARE.encode()).hexdigest()
        self.assertEqual(canonical_target_urn(post()), SHARE)
        self.assertEqual(comment_key(post(), {}), expected)
        self.assertEqual(comment_key(post(url='https://www.linkedin.com/feed/update/' + SHARE + '/'), {}), expected)

    def test_legacy_url_hash_is_preserved_after_canonical_discovery(self):
        legacy = 'comment:' + hashlib.sha256(URL.encode()).hexdigest()
        state = {'actions': {'reaction:' + legacy: {'kind': 'reaction', 'post_urn': SHARE, 'status': 'sent'}}}
        self.assertEqual(comment_key(post(), state), legacy)
        state = {'actions': {legacy: {'kind': 'comment', 'post_urn': SHARE, 'status': 'sent'}}}
        alternative = post(url='https://www.linkedin.com/feed/update/' + SHARE + '/')
        self.assertEqual(comment_key(alternative, state), legacy)

    def test_cache_merges_different_activity_urls_by_explicit_share_urn(self):
        state = {}
        other_url = 'https://www.linkedin.com/feed/update/urn:li:activity:7000000000000000002/'
        stash_targets(at(), POLICY, state, [post(), post(url=other_url)])
        self.assertEqual(len(candidate_targets(at(), POLICY, state)), 1)
        self.assertEqual(set(state['engagement']['targets'][0]['identity_aliases']),
                         {ACTIVITY, SHARE, 'urn:li:activity:7000000000000000002'})

    def test_activity_only_refresh_does_not_lose_previous_writable_share(self):
        state = {}
        stash_targets(at(), POLICY, state, [post()])
        stash_targets(at(9), POLICY, state, [post(urn=ACTIVITY)])
        self.assertEqual(len(state['engagement']['targets']), 1)
        self.assertEqual(state['engagement']['targets'][0]['urn'], SHARE)

    def test_legacy_uncertain_activity_reaction_quarantines_canonical_target(self):
        legacy = 'comment:' + hashlib.sha256(URL.encode()).hexdigest()
        for receipt in ({'kind': 'reaction', 'status': 'unknown-needs-reconciliation', 'post_urn': ACTIVITY},
                        {'kind': 'reaction', 'status': 'inflight'}):
            state = {'actions': {'reaction:' + legacy: receipt}}
            stash_targets(at(), POLICY, state, [post()])
            self.assertEqual(candidate_targets(at(), POLICY, state), [])

    def test_completed_target_is_not_returned_after_restart_with_alternate_url(self):
        key = 'comment:' + hashlib.sha256(URL.encode()).hexdigest()
        state = {'actions': {key: {'kind': 'comment', 'post_urn': SHARE, 'status': 'sent'},
                             'reaction:' + key: {'kind': 'reaction', 'post_urn': SHARE, 'status': 'sent'}}}
        stash_targets(at(), POLICY, state, [post(url='https://www.linkedin.com/feed/update/' + SHARE + '/')])
        self.assertEqual(candidate_targets(at(), POLICY, json.loads(json.dumps(state))), [])

    def test_rejected_comment_is_not_retried_but_missing_like_can_be_written(self):
        key = comment_key(post(), {})
        state = {'actions': {key: {'kind': 'comment', 'post_urn': SHARE, 'status': 'rejected'}}}
        stash_targets(at(), POLICY, state, [post()])
        self.assertEqual(len(candidate_targets(at(), POLICY, state)), 1)
        state['actions']['reaction:' + key] = {'kind': 'reaction', 'post_urn': SHARE, 'status': 'rejected'}
        self.assertEqual(candidate_targets(at(), POLICY, state), [])

    def test_own_posts_and_unsafe_targets_never_enter_cache(self):
        state = {}
        stash_targets(at(), POLICY, state, [
            post(authorProfileUrl='https://linkedin.com/in/OWNER/?trk=feed'),
            post(url='https://linkedin.com.attacker.test/feed/update/' + ACTIVITY),
            post(text='   '), {'url': URL, 'text': 'No stable identity'},
        ])
        # A validated post URL itself is also a usable activity identity.
        self.assertEqual(len(state['engagement']['targets']), 1)
        self.assertEqual(state['engagement']['targets'][0]['urn'], ACTIVITY)

    def test_known_old_posts_expire_while_recent_posts_remain(self):
        state = {}
        stash_targets(at(), POLICY, state, [post(postedAtISO=(at() - timedelta(days=7)).isoformat())])
        self.assertEqual(len(candidate_targets(at(), POLICY, state)), 1)
        self.assertEqual(candidate_targets(at() + timedelta(seconds=1), POLICY, state), [])

    def test_unknown_post_age_expires_from_first_discovery(self):
        state = {}
        stash_targets(at(), POLICY, state, [post(postedAtISO=None)])
        stash_targets(at() + timedelta(days=6), POLICY, state, [post(postedAtISO=None)])
        self.assertEqual(state['engagement']['targets'][0]['first_seen_at'], at().astimezone(timezone.utc).isoformat())
        self.assertEqual(candidate_targets(at() + timedelta(days=7, seconds=1), POLICY, state), [])

    def test_cache_bound_and_external_metadata_are_not_copied(self):
        state = {}
        for group in range(3):
            rows = [post(urn='urn:li:share:' + str(7000000000000000000 + group * 50 + item),
                         url='https://www.linkedin.com/feed/update/urn:li:share:' + str(7000000000000000000 + group * 50 + item) + '/',
                         providerSecret='must-not-be-copied') for item in range(50)]
            stash_targets(at(), POLICY, state, rows)
        self.assertEqual(len(state['engagement']['targets']), 100)
        self.assertNotIn('providerSecret', json.dumps(state))

    def test_unsuitable_target_deferral_survives_restart_and_expires(self):
        state = {}
        stash_targets(at(), POLICY, state, [post()])
        mark_deferred(at(), state, post())
        persisted = json.loads(json.dumps(state))
        self.assertEqual(candidate_targets(at(9), POLICY, persisted), [])
        self.assertEqual(len(candidate_targets(at() + timedelta(days=1), POLICY, persisted)), 1)


class ProfileCacheTests(unittest.TestCase):
    def test_cache_suppresses_five_minute_scrapes_and_allows_six_hour_refresh(self):
        state = {}
        self.assertTrue(should_discover(at(), POLICY, state))
        mark_discovered(at(), POLICY, state, 'source')
        persisted = json.loads(json.dumps(state))
        self.assertFalse(should_discover(at(8, 5), POLICY, persisted))
        self.assertFalse(should_discover(at(13, 59), POLICY, persisted))
        self.assertTrue(should_discover(at(14), POLICY, persisted))

    def test_rotation_matches_existing_profile_discovery_and_excludes_owner(self):
        policy = {**POLICY, 'discovery_profiles': ['OWNER', 'source', 'SOURCE', 'other']}
        state = {}
        self.assertEqual(next_discovery_profile(policy, state), 'https://www.linkedin.com/in/source/')
        mark_discovered(at(), policy, state, 'source')
        state['discovery'] = {'profile_cursor': 1}
        self.assertEqual(next_discovery_profile(policy, state), 'https://www.linkedin.com/in/other/')
        self.assertTrue(should_discover(at(8, 5), policy, state))
        mark_discovered(at(8, 5), policy, state, 'other')
        self.assertFalse(should_discover(at(8, 10), policy, state))

    def test_per_profile_cache_remains_independent(self):
        state = {}
        stash_targets(at(), POLICY, state, [post()], source_profile='source')
        self.assertFalse(should_discover(at(8, 5), POLICY, state, 'source'))
        self.assertTrue(should_discover(at(8, 5), POLICY, state, 'other'))

    def test_no_valid_external_sources_means_no_discovery(self):
        self.assertFalse(should_discover(at(), {**POLICY, 'discovery_profiles': ['owner']}, {}))
        with self.assertRaises(ValueError):
            mark_discovered(at(), POLICY, {}, 'owner')
        with self.assertRaises(ValueError):
            mark_discovered(at(), POLICY, {}, 'https://attacker.test/in/source')


if __name__ == '__main__':
    unittest.main()
