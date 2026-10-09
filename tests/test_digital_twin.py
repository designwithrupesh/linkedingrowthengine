import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from automation.twin import Runner, flatten_comments, preflight, model, main, LocalModelError, ModelConfigurationError, ModelQualityError, WriteOutcomeError, WriteCheckpointError, normalize_public_text, validate_model_output, skills, compact_skill_context

# Preserve the original single-slot/shared-interaction contracts independently
# of the owner's current, expanded live policy. New cadence and reply limits
# have their own integration coverage in test_expanded_runner.py.
POLICY = {
    'timezone': 'Asia/Calcutta',
    'platform_id': 'linkedin-tYCSPeVNvi',
    'profile_url': 'https://www.linkedin.com/in/therupeshkumar/',
    'background': ['creative direction', 'product design', 'startup founding', 'brand strategy'],
    'goals': ['consulting clients', 'personal brand', 'founding designer roles'],
    'audience': 'early-stage founders and product leaders',
    'topics': ['product design decisions', 'brand strategy in product experiences'],
    'posting_days': [0, 1, 2, 3, 4],
    'posting_hour': 9,
    'max_posts_per_day': 1,
    'max_interactions_per_day': 5,
    'max_read_calls_per_day': 4,
    'target_post_urls': [],
    'source_notes': [],
    'team_members': [],
    'authorization': 'Owner authorized routine design content and contextual comments/replies.',
    'boundaries': 'Do not invent career stories, clients, metrics, quotes or commitments.',
    'react_to_target_posts': True,
    'discovery_profiles': [],
    'free_read_credit_reserve_usd': 1,
}
NOW = datetime(2026, 10, 13, 3, 30, tzinfo=timezone.utc)
TEXT = 'A concrete design observation.'

class DigitalTwinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.json'
        self.enterContext(patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': ''}))
    def tearDown(self):
        self.temp.cleanup()
    def runner(self, **kw):
        return Runner(POLICY, self.path, now=NOW, generate=lambda *a: {'text': TEXT}, capacity=lambda *a: (True, 'capacity verified'), **kw)
    def test_full_remote_queue_defers_without_generating_or_consuming_intent(self):
        r = self.runner(live=True)
        r.post_capacity = lambda *a: (False, 'Queue is full')
        r.generate = Mock()
        with patch('automation.twin.requests.post') as send:
            r.post()
            r.write('post:2026-10-14', 'post', TEXT, scheduled_time=NOW + timedelta(days=1))
        r.generate.assert_not_called()
        send.assert_not_called()
        self.assertFalse(r.state['actions'])
        self.assertEqual(r.counts['post'], 0)
        self.assertNotIn('2026-10-14', r.state['days'])
        r.post_capacity = lambda *a: (True, 'capacity verified')
        r.generate = lambda *a: {'text': TEXT}
        with patch.object(r, 'write') as write:
            r.post()
        write.assert_called_once()
    def test_restart_does_not_duplicate_post(self):
        self.runner().post()
        self.runner().post()
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))['actions']), 1)
    def test_uses_india_posting_window(self):
        r = Runner(POLICY, self.path, now=datetime(2026,10,13,3,29,tzinfo=timezone.utc), generate=Mock())
        r.post()
        r.generate.assert_not_called()
        self.assertEqual(self.runner().local.hour, 9)
    def test_daily_interaction_cap(self):
        r = self.runner()
        for i in range(12):
            r.write(str(i), 'comment', TEXT, 'urn:li:activity:123')
        self.assertEqual(len(r.state['actions']), 5)
    def test_skip_does_not_publish(self):
        r = self.runner()
        r.generate = lambda *a: {'skip': True}
        r.post()
        self.assertFalse(r.state['actions'])
    def test_preview_fails_when_model_skips_instead_of_claiming_readiness(self):
        with patch('automation.twin.model', return_value={'skip': True}), \
             patch('automation.twin.requests.get') as read, \
             patch('automation.twin.requests.post') as publish, \
             patch('sys.argv', ['twin', '--preview', '--state', str(self.path)]), \
             patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ModelQualityError):
                main()
        read.assert_not_called()
        publish.assert_not_called()
    def test_nested_parent_and_own_comment_filter(self):
        rows = [{'comment_id':'1','author':{'profile_url':POLICY['profile_url']},'replies':[{'comment_id':'2','text':'question','author':{'profile_url':'https://www.linkedin.com/in/other/'}}]}]
        result = flatten_comments(rows, POLICY['profile_url'])
        self.assertEqual([(c['id'],c['parent_id']) for c in result], [('2','1')])
    def test_nested_reply_preserves_parent_context_and_normalizes_own_identity(self):
        rows = [{'comment_id': '1', 'text': 'What did you prioritize?', 'author': {'name': 'Founder',
            'profile_url': 'https://www.linkedin.com/in/founder/'}, 'replies': [
            {'comment_id': '2', 'text': 'My own answer', 'author': {'name': 'Rupesh',
             'profile_url': 'https://LINKEDIN.COM/in/TheRupeshKumar/?trk=feed#comment'}},
            {'comment_id': '3', 'text': 'Why that first?', 'author': {'name': 'Another person',
             'profile_url': 'https://www.linkedin.com/in/another/'}}]}]
        result = flatten_comments(rows, POLICY['profile_url'])
        self.assertEqual([c['id'] for c in result], ['1', '3'])
        self.assertEqual(result[-1]['parent_text'], 'What did you prioritize?')
        self.assertEqual(result[-1]['parent_author'], 'Founder')
        self.assertEqual(result[-1]['parent_id'], '1')

    @patch('automation.twin.requests.post')
    def test_future_schedule_uses_publish_day_limits_and_actual_request_time(self, send):
        send.return_value.status_code = 200
        send.return_value.json.return_value = {'postGroupId': 'scheduled-group'}
        schedule = datetime(2026, 10, 14, 9, 0, tzinfo=ZoneInfo(POLICY['timezone']))
        r = self.runner(live=True)
        with patch.object(r, 'save'), patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}):
            r.write('post:2026-10-14', 'post', TEXT, scheduled_time=schedule, allocation_day='2026-10-14')
            r.write('second-post', 'post', TEXT, scheduled_time=schedule + timedelta(hours=1), allocation_day='2026-10-14')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.kwargs['json']['scheduledTime'], '2026-10-14T03:30:00Z')
        self.assertEqual(r.counts['post'], 0)
        self.assertEqual(r.state['days']['2026-10-14']['post'], 1)
        action = r.state['actions']['post:2026-10-14']
        self.assertEqual(action['at'], NOW.isoformat())
        self.assertEqual(action['scheduled_for'], '2026-10-14T03:30:00+00:00')
        self.assertEqual(r.state['posts'][0]['allocation_day'], '2026-10-14')

    def test_future_schedule_derives_local_allocation_day_and_survives_restart(self):
        # UTC evening already belongs to the next date in India.
        schedule = datetime(2026, 10, 13, 20, 0, tzinfo=timezone.utc)
        r = self.runner()
        r.write('future', 'post', TEXT, scheduled_time=schedule)
        restarted = self.runner()
        restarted.write('duplicate-day', 'post', TEXT, scheduled_time=schedule + timedelta(hours=1))
        self.assertEqual(list(restarted.state['actions']), ['future'])
        self.assertEqual(restarted.state['actions']['future']['allocation_day'], '2026-10-14')

    @patch('automation.twin.requests.post')
    def test_invalid_future_schedule_never_persists_or_sends(self, send):
        for options in ({'scheduled_time': NOW.replace(tzinfo=None)},
                        {'scheduled_time': NOW},
                        {'scheduled_time': NOW - timedelta(minutes=1)},
                        {'scheduled_time': NOW + timedelta(days=1), 'allocation_day': '2026-10-15'}):
            with self.subTest(options=options):
                r = self.runner(live=True)
                with self.assertRaises(ValueError):
                    r.write('invalid', 'post', TEXT, **options)
                self.assertFalse(r.state['actions'])
        send.assert_not_called()

    def test_interactions_cannot_bypass_daily_caps_by_allocating_another_day(self):
        with self.assertRaises(ValueError):
            self.runner().write('invalid', 'comment', TEXT, allocation_day='2026-10-14')
    @patch('automation.twin.requests.post')
    def test_dry_run_never_sends(self, send):
        self.runner().post()
        send.assert_not_called()
    @patch('automation.twin.requests.post')
    def test_failed_checkpoint_prevents_remote_write(self, send):
        r = self.runner(live=True)
        with patch.object(r, 'save', side_effect=RuntimeError('push failed')):
            with self.assertRaises(RuntimeError):
                r.write('a', 'post', TEXT)
        send.assert_not_called()
    @patch('automation.twin.requests.post')
    def test_ambiguous_write_is_recorded_and_never_retried(self, send):
        send.side_effect = TimeoutError()
        r = self.runner(live=True)
        with patch.object(r, 'save'), patch.dict(os.environ, {'PUBLORA_API_KEY':'test-placeholder'}):
            with self.assertRaises(RuntimeError):
                r.write('a', 'post', TEXT)
            r.write('a', 'post', TEXT)
        self.assertEqual(send.call_count,1)
        self.assertEqual(r.state['actions']['a']['status'], 'unknown-needs-reconciliation')
        self.assertIsNone(r.state['actions']['a']['http_status'])

    @patch('automation.twin.requests.post')
    def test_definitive_rejection_allows_next_intent_and_never_reads_provider_body(self, send):
        rejected = Mock(status_code=403)
        rejected.json.return_value = {'error': 'private provider text'}
        accepted = Mock(status_code=201)
        accepted.json.return_value = {'success': True, 'comment': {'id': 'comment-123'}}
        send.side_effect = [rejected, accepted]
        r = self.runner(live=True)
        with patch.object(r, 'save'), patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}), patch('builtins.print') as log:
            r.write('reaction', 'reaction', 'LIKE', 'urn:li:share:7000000000000000000')
            r.write('reaction', 'reaction', 'LIKE', 'urn:li:share:7000000000000000000')
            r.write('comment', 'comment', TEXT, 'urn:li:share:7000000000000000000')
        self.assertEqual(send.call_count, 2)
        self.assertEqual(r.state['actions']['reaction']['status'], 'rejected')
        self.assertEqual(r.state['actions']['reaction']['http_status'], 403)
        self.assertEqual(r.state['actions']['comment']['status'], 'sent')
        self.assertEqual(r.counts['interaction'], 2)
        rejected.json.assert_not_called()
        self.assertIn('HTTP 403', log.call_args.args[0])
        self.assertNotIn('private provider text', str(r.state))
        self.assertFalse(send.call_args.kwargs['allow_redirects'])

    @patch('automation.twin.requests.post')
    def test_nondefinitive_http_response_is_unknown_with_safe_status_and_no_retry(self, send):
        for status in (307, 408, 429, 500, 503):
            with self.subTest(status=status):
                send.reset_mock()
                send.return_value = Mock(status_code=status)
                send.return_value.json.return_value = {'error': 'sensitive response body'}
                r = self.runner(live=True)
                with patch.object(r, 'save'), patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}):
                    with self.assertRaises(WriteOutcomeError) as caught:
                        r.write('uncertain', 'comment', TEXT, 'urn:li:share:7000000000000000000')
                    r.write('uncertain', 'comment', TEXT, 'urn:li:share:7000000000000000000')
                self.assertEqual(send.call_count, 1)
                self.assertEqual(r.state['actions']['uncertain']['status'], 'unknown-needs-reconciliation')
                self.assertEqual(r.state['actions']['uncertain']['http_status'], status)
                self.assertIn('HTTP ' + str(status), str(caught.exception))
                self.assertNotIn('sensitive response body', str(caught.exception))
                send.return_value.json.assert_not_called()

    @patch('automation.twin.requests.post')
    def test_missing_acknowledgement_stays_blocked_after_restart(self, send):
        send.return_value = Mock(status_code=200)
        send.return_value.json.return_value = {'success': True, 'message': 'provider text'}
        r = self.runner(live=True)
        def persist():
            r.path.write_text(json.dumps(r.state), encoding='utf-8')
        with patch.object(r, 'save', side_effect=persist), patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}):
            with self.assertRaises(WriteOutcomeError):
                r.write('uncertain', 'comment', TEXT, 'urn:li:share:7000000000000000000')
        restarted = self.runner(live=True)
        restarted.write('uncertain', 'comment', TEXT, 'urn:li:share:7000000000000000000')
        self.assertEqual(send.call_count, 1)
        self.assertEqual(restarted.state['actions']['uncertain']['http_status'], 200)
        self.assertEqual(restarted.counts['interaction'], 1)
        self.assertNotIn('provider text', str(restarted.state))

    @patch('automation.twin.requests.post')
    def test_failed_ack_checkpoint_preserves_known_remote_success(self, send):
        send.return_value = Mock(status_code=201)
        send.return_value.json.return_value = {'success': True, 'postGroupId': 'group-123'}
        r = self.runner(live=True)
        with patch.object(r, 'save', side_effect=[None, RuntimeError('push failed')]), \
             patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}):
            with self.assertRaises(WriteCheckpointError):
                r.write('scheduled', 'post', TEXT)
            r.write('scheduled', 'post', TEXT)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(r.state['actions']['scheduled']['status'], 'scheduled')
        self.assertEqual(r.state['actions']['scheduled']['remote_id'], 'group-123')
        self.assertEqual(r.state['posts'][0]['id'], 'group-123')

    @patch('automation.twin.requests.post')
    def test_uncertain_reaction_does_not_block_independent_post(self, send):
        send.return_value = Mock(status_code=201)
        send.return_value.json.return_value = {'success': True, 'postGroupId': 'new-post'}
        r = self.runner(live=True)
        r.state['actions']['old-reaction'] = {'kind': 'reaction', 'status': 'unknown-needs-reconciliation', 'text': 'LIKE'}
        with patch.object(r, 'save'), patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}):
            r.post()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(r.state['actions']['post:' + r.day]['status'], 'scheduled')

    def test_uncertain_reaction_quarantines_its_target_before_read_or_generation(self):
        url = 'https://www.linkedin.com/feed/update/urn:li:share:7000000000000000000/'
        r = self.runner()
        r.policy = dict(POLICY, target_post_urls=[url])
        key = 'reaction:comment:' + hashlib.sha256(url.encode()).hexdigest()
        r.state['actions'][key] = {'kind': 'reaction', 'status': 'unknown-needs-reconciliation'}
        reader = Mock()
        r.generate = Mock()
        r.interactions(reader)
        reader.fetch_post.assert_not_called()
        r.generate.assert_not_called()
    @patch('automation.twin.requests.get')
    def test_missing_secret_stops_before_network(self, get):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                preflight(POLICY)
        get.assert_not_called()
    @patch('automation.twin.requests.get')
    @patch('automation.twin.Runner.post')
    def test_missing_apify_binding_stops_main_before_any_post(self, publish, get):
        with patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder', 'GITHUB_TOKEN': 'test-placeholder',
                                    'TWIN_LIVE': 'true', 'TWIN_READ_ENABLED': 'true'}, clear=True), \
             patch('sys.argv', ['twin']):
            with self.assertRaisesRegex(RuntimeError, 'APIFY_TOKEN'):
                main()
        publish.assert_not_called()
        get.assert_not_called()

    @patch('automation.twin.requests.get')
    @patch('automation.twin.Runner.post')
    def test_invalid_apify_binding_stops_main_before_any_post(self, publish, get):
        from requests import HTTPError
        channel = Mock()
        channel.json.return_value = {'connections': [{'platformId': POLICY['platform_id']}]}
        denied = Mock()
        denied.raise_for_status.side_effect = HTTPError('Unauthorized')
        get.side_effect = [channel, denied]
        with patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder', 'GITHUB_TOKEN': 'test-placeholder',
                                    'APIFY_TOKEN': 'invalid-test-placeholder', 'TWIN_LIVE': 'true',
                                    'TWIN_READ_ENABLED': 'true'}, clear=True), patch('sys.argv', ['twin']):
            with self.assertRaises(HTTPError):
                main()
        publish.assert_not_called()
        self.assertEqual(get.call_args.args[0], 'https://api.apify.com/v2/users/me')
    @patch('automation.twin.requests.get')
    def test_wrong_channel_blocks_preflight(self, get):
        get.return_value.json.return_value = {'connections':[{'platformId':'linkedin-other'}]}
        with patch.dict(os.environ, {'PUBLORA_API_KEY':'test-placeholder','GITHUB_TOKEN':'test-placeholder'}, clear=True):
            with self.assertRaises(RuntimeError):
                preflight(POLICY)
    @patch('automation.twin.requests.get')
    def test_discover_published_uses_post_id_path_and_saves_permalink(self, get):
        url = 'https://www.linkedin.com/feed/update/urn:li:share:7000000000000000000/'
        get.return_value.json.return_value = {'posts': [{'platform': 'linkedin', 'status': 'published', 'permalink': url}]}
        r = self.runner()
        r.state['posts'].append({'id': 'group/with?reserved#characters', 'url': None})
        with patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}):
            r.discover_published()
        get.assert_called_once_with(
            'https://api.publora.com/api/v1/get-post/group%2Fwith%3Freserved%23characters',
            headers={'x-publora-key': 'test-placeholder'}, timeout=30)
        get.return_value.raise_for_status.assert_called_once_with()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved['posts'][0]['url'], url)

    @patch('automation.twin.requests.get')
    def test_discover_published_builds_url_from_real_null_permalink_response(self, get):
        payload = json.loads(Path('tests/fixtures/publora_get_post.json').read_text(encoding='utf-8'))
        self.assertIsNone(payload['posts'][0]['permalink'])
        get.return_value.json.return_value = payload
        r = self.runner()
        r.state['posts'].append({'id': payload['postGroupId'], 'url': None, 'text': payload['posts'][0]['content']})
        with patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}):
            r.discover_published()
        saved = json.loads(self.path.read_text(encoding='utf-8'))['posts'][0]
        urn = payload['posts'][0]['postedId']
        self.assertEqual(saved['post_urn'], urn)
        self.assertEqual(saved['url'], 'https://www.linkedin.com/feed/update/' + urn + '/')
        reader = Mock()
        reader.fetch_post_comments.return_value = [{'comment_id': '42', 'text': 'How would you prioritize this?',
            'author': {'profile_url': 'https://www.linkedin.com/in/other/'},
            'posted_at': {'timestamp': int(NOW.timestamp() * 1000)}}]
        r.write = Mock()
        r.interactions(reader)
        reader.fetch_post_comments.assert_called_once_with(post_id=urn, max_items=20)
        self.assertEqual(r.write.call_args.args[3:], (urn, 'urn:li:comment:(' + urn + ',42)'))

    @patch('automation.twin.requests.get')
    def test_discover_published_ignores_unpublished_and_other_platform_children(self, get):
        url = 'https://www.linkedin.com/feed/update/urn:li:share:7000000000000000000/'
        get.return_value.json.return_value = {'posts': [
            {'platform': 'twitter', 'status': 'published', 'permalink': url},
            {'platform': 'linkedin', 'status': 'scheduled', 'permalink': url},
            {'platform': 'linkedin', 'status': 'published', 'platformId': 'linkedin-other', 'permalink': url},
            {'platform': 'linkedin', 'status': 'published', 'postedId': '7000000000000000000'},
        ], 'content': url}
        r = self.runner()
        r.state['posts'].append({'id': 'group', 'url': None})
        with patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}):
            r.discover_published()
        self.assertIsNone(r.state['posts'][0]['url'])

    def test_target_interaction_prefers_normalized_urn_over_activity_url(self):
        url = 'https://www.linkedin.com/posts/person_activity-7000000000000000000-abcd'
        canonical = 'urn:li:ugcPost:7000000000000000001'
        for fields in ({'urn': canonical, 'shareUrn': 'urn:li:share:7000000000000000002'},
                       {'shareUrn': canonical}):
            with self.subTest(fields=fields):
                r = self.runner()
                r.policy = dict(POLICY, target_post_urls=[url])
                reader = Mock()
                reader.fetch_post.return_value = {'text': 'A real design post', **fields}
                r.write = Mock()
                r.interactions(reader)
                self.assertEqual(r.write.call_args_list[-1].args[3], canonical)

    def test_target_interaction_advances_beyond_previously_handled_first_two(self):
        urls = ['https://www.linkedin.com/posts/person_activity-' + str(7000000000000000000 + i) + '-abcd'
                for i in range(5)]
        r = self.runner()
        r.policy = dict(POLICY, target_post_urls=urls)
        for url in urls[:2]:
            key = 'comment:' + hashlib.sha256(url.encode()).hexdigest()
            r.state['actions'][key] = {'kind': 'comment', 'status': 'sent'}
        reader = Mock()
        reader.fetch_post.return_value = {'text': 'A real design post', 'urn': 'urn:li:share:7000000000000000000'}
        r.write = Mock()
        r.interactions(reader)
        self.assertEqual([call.args[0] for call in reader.fetch_post.call_args_list], urls[2:4])
    @patch('automation.twin.requests.post')
    def test_empty_model_variables_use_defaults(self, post):
        post.return_value.json.return_value={'choices':[{'message':{'content':'{"skip":true}'}}]}
        with patch.dict(os.environ, {'MODEL_API_KEY':'test-placeholder','MODEL_ENDPOINT':'','MODEL_NAME':''}, clear=True):
            model('task', {}, POLICY)
        self.assertEqual(post.call_args.args[0], 'https://api.openai.com/v1/chat/completions')
        self.assertEqual(post.call_args.kwargs['json']['model'], 'gpt-4.1-mini')

    @patch('automation.twin.complete')
    def test_failed_draft_receives_one_source_only_edit_pass(self, complete):
        source = 'A design decision should clarify what an early team needs to learn. ' * 5
        edited = ('Research should inform the next design decision. A prototype can expose an uncertain assumption before a team commits to the full product. ' * 8).strip()
        self.assertTrue(900 <= len(edited) <= 1300)
        complete.side_effect = [{'text': source, 'skip': False}, {'text': edited, 'skip': False}]
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-placeholder'}, clear=True):
            result = model(skills('linkedin-post-writer'), {'topic': 'design decisions', 'private_context': 'original input'}, POLICY)
        self.assertFalse(result['skip'])
        self.assertEqual(complete.call_count, 2)
        editor_messages = complete.call_args.args[-1]
        self.assertEqual(json.loads(editor_messages[1]['content'])['draft'], source)
        self.assertNotIn('original input', json.dumps(editor_messages))
        self.assertIn('linkedin-humanizer', editor_messages[0]['content'])

    @patch('automation.twin.complete')
    def test_failed_editor_is_skipped_without_padding_or_more_calls(self, complete):
        short = 'A useful design trade-off deserves attention. ' * 5
        complete.return_value = {'text': short, 'skip': False}
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-placeholder'}, clear=True):
            result = model(skills('linkedin-post-writer'), {'topic': 'design'}, POLICY)
        self.assertTrue(result['skip'])
        self.assertEqual(result['text'], '')
        self.assertEqual(complete.call_count, 2)

    @patch('automation.twin.complete')
    def test_editor_cannot_introduce_numeric_claims(self, complete):
        source = 'Product research should inform the next design decision. ' * 5
        edited = ('A prototype exposes an uncertain assumption before the team commits to building the full product. ' * 10) + 'Conversion increased by 50%.'
        self.assertTrue(900 <= len(edited) <= 1300)
        complete.side_effect = [{'text': source, 'skip': False}, {'text': edited, 'skip': False}]
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-placeholder'}, clear=True):
            result = model(skills('linkedin-post-writer'), {'topic': 'design'}, POLICY)
        self.assertTrue(result['skip'])
        self.assertEqual(result['reason'], 'Editor introduced new numeric claims')

    @patch('automation.twin.requests.post')
    def test_compatible_model_fenced_json(self, post):
        post.return_value.json.return_value={'choices':[{'message':{'content':'```json\n{"text":"draft","skip":false}\n```'}, 'finish_reason':'stop'}]}
        with patch.dict(os.environ, {'MODEL_API_KEY':'test-placeholder'}, clear=True):
            result = model('task', {}, POLICY)
        self.assertEqual(result['text'], 'draft')

    @patch('automation.twin.requests.post')
    def test_local_model_requires_no_token_and_never_forwards_real_credentials(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {'choices': [{'message': {'content': '{"text":"draft","skip":false}'}}]}
        with patch.dict(os.environ, {'MODEL_ENDPOINT': 'http://127.0.0.1:8080/v1/chat/completions',
                                    'MODEL_API_KEY': 'private-custom-placeholder', 'GITHUB_TOKEN': 'private-github-placeholder'}, clear=True):
            self.assertEqual(model('task', {}, POLICY)['text'], 'draft')
        self.assertEqual(post.call_args.kwargs['headers'], {'Authorization': 'Bearer twin-local'})
        self.assertEqual(post.call_args.kwargs['json']['model'], 'twin-local')
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    @patch('automation.twin.requests.get')
    def test_local_model_preflight_does_not_require_github_or_model_key(self, get):
        get.return_value.json.return_value = {'connections': [{'platformId': POLICY['platform_id']}]}
        with patch.dict(os.environ, {'MODEL_ENDPOINT': 'http://127.0.0.1:8080/v1/chat/completions',
                                    'PUBLORA_API_KEY': 'test-placeholder'}, clear=True):
            preflight(POLICY)
        self.assertEqual(get.call_count, 1)

    @patch('automation.twin.requests.post')
    def test_custom_model_endpoint_never_uses_github_token(self, post):
        for endpoint in ('https://example.com/chat/completions',
                         'https://models.github.ai.evil.example/inference/chat/completions',
                         'https://models.github.ai/other-path'):
            with self.subTest(endpoint=endpoint), \
                 patch.dict(os.environ, {'MODEL_ENDPOINT': endpoint, 'GITHUB_TOKEN': 'private-github-placeholder'}, clear=True):
                with self.assertRaises(ModelConfigurationError):
                    model('task', {}, POLICY)
        post.assert_not_called()

    @patch('automation.twin.requests.post')
    def test_only_supported_loopback_endpoint_may_use_plain_http(self, post):
        for endpoint in ('http://example.com/chat/completions', 'http://127.0.0.1.evil.example:8080/v1/chat/completions',
                         'http://127.0.0.1:8080/other-path'):
            with self.subTest(endpoint=endpoint), \
                 patch.dict(os.environ, {'MODEL_ENDPOINT': endpoint, 'MODEL_API_KEY': 'test-placeholder'}, clear=True):
                with self.assertRaises(ModelConfigurationError):
                    model('task', {}, POLICY)
        post.assert_not_called()

    @patch('automation.twin.requests.post')
    def test_unavailable_local_model_raises_sanitized_readiness_error(self, post):
        from requests import ConnectionError
        post.side_effect = ConnectionError('provider body with private data')
        with patch.dict(os.environ, {'MODEL_ENDPOINT': 'http://127.0.0.1:8080/v1/chat/completions'}, clear=True):
            with self.assertRaisesRegex(LocalModelError, 'Local model server is unavailable') as caught:
                model('task', {}, POLICY)
        self.assertNotIn('private data', str(caught.exception))

    def test_public_format_cleanup_preserves_facts_and_named_tools(self):
        draft = '## 🎨 **Design trade-offs**\n\nThe C# prototype cost $4,730 on 14 Feb.\n\n`Figma` made #Design reviews clearer. #ProductDesign #Startups\n\n#FoundingDesigner #BrandStrategy'
        cleaned = normalize_public_text(draft)
        self.assertEqual(cleaned, 'Design trade-offs\n\nThe C# prototype cost $4,730 on 14 Feb.\n\nFigma made Design reviews clearer.')

    def test_invalid_short_post_and_long_comment_are_skipped_without_padding(self):
        short = 'A truthful design observation. ' * 10
        self.assertLess(len(short), 400)
        result = validate_model_output({'text': short, 'skip': False}, 'post')
        self.assertTrue(result['skip'])
        self.assertEqual(result['text'], '')
        self.assertTrue(validate_model_output({'text': short * 2, 'skip': False}, 'comment')['skip'])

    def test_checklist_output_is_skipped_instead_of_rewritten_into_claims(self):
        draft = 'Design choices deserve context.\n\n1. ' + 'Discuss the trade-off with your team. ' * 28
        self.assertTrue(validate_model_output({'text': draft, 'skip': False}, 'post')['skip'])

    def test_local_compaction_retains_all_operational_skill_names_and_hard_rules(self):
        full = skills('linkedin-content-planner', 'linkedin-post-writer', 'linkedin-humanizer')
        compact = compact_skill_context(full)
        self.assertLess(len(compact), len(full))
        for name in ('linkedin-content-planner', 'linkedin-post-writer', 'linkedin-humanizer'):
            self.assertIn('Skill: ' + name, compact)
        self.assertIn('## Non-negotiable rules', compact)
        self.assertIn('## Hard rules', compact)

    @patch('automation.twin.requests.post')
    def test_action_constraints_follow_runtime_overrides_and_use_text_length(self, post):
        comment = ('A useful design review distinguishes a reversible interface choice from a product promise. '
                   'That distinction helps an early team decide what to test quickly and what needs a clear owner before shipping.')
        post.return_value.json.return_value = {'choices': [{'message': {'content': json.dumps({'text': comment, 'skip': False})}}]}
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-placeholder'}, clear=True):
            result = model(skills('linkedin-comment-drafter'), {'post': {'text': 'Product teams need ownership'}}, POLICY)
        self.assertFalse(result['skip'])
        prompt = post.call_args.kwargs['json']['messages'][0]['content']
        self.assertGreater(prompt.rfind('FINAL OUTPUT REQUIREMENTS'), prompt.index('Skill: linkedin-comment-drafter'))
        self.assertIn('80-350 characters in text', prompt)
        self.assertNotIn('900-1300 characters', prompt[prompt.rfind('FINAL OUTPUT REQUIREMENTS'):])
        self.assertEqual(post.call_count, 1)

    @patch('automation.twin.requests.post')
    def test_short_concrete_comment_is_accepted_without_padding_or_an_edit_call(self, post):
        comment = ('Compare both prototypes on the same customer task. If the faster tool leaves users '
                   'equally confused, it improves production speed without improving the decision about what to build.')
        self.assertGreaterEqual(len(comment), 140)
        self.assertLess(len(comment), 200)
        post.return_value.json.return_value = {'choices': [{'message': {'content': json.dumps({'text': comment, 'skip': False})}}]}
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-placeholder'}, clear=True):
            result = model(skills('linkedin-comment-drafter'),
                           {'post': {'text': 'Does a faster prototyping tool improve the customer experience?'}}, POLICY)
        self.assertFalse(result['skip'])
        self.assertEqual(result['text'], comment)
        post.assert_called_once()

    @patch('automation.twin.requests.post')
    def test_analysis_generation_avoids_post_style_and_length_requirements(self, post):
        report = 'The available commenter headlines include product design and early-stage leadership. This supports audience relevance, but company size and consulting intent are unknown from the supplied records.'
        post.return_value.json.return_value = {'choices': [{'message': {'content': json.dumps({'text': report, 'skip': False})}}]}
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-placeholder'}, clear=True):
            result = model(skills('linkedin-engager-analytics'), {'engagers': []}, POLICY)
        self.assertFalse(result['skip'])
        prompt = post.call_args.kwargs['json']['messages'][0]['content']
        final_rules = prompt[prompt.rfind('FINAL OUTPUT REQUIREMENTS'):]
        self.assertIn('audience-fit analysis', final_rules)
        self.assertNotIn('900-1300', final_rules)

    def test_discovered_target_uses_existing_context_without_second_read(self):
        policy = {**POLICY, 'discovery_profiles':['source-designer'], 'target_post_urls': []}
        r = Runner(policy, self.path, now=NOW, generate=lambda *a: {'text':TEXT})
        url='https://www.linkedin.com/feed/update/urn:li:share:7000000000000000000/'
        reader=Mock()
        reader.fetch_profile_posts.return_value=[{'url':url,'urn':'urn:li:share:7000000000000000000','text':'Product-design context','authorProfileUrl':'https://www.linkedin.com/in/source-designer/'}]
        r.interactions(reader)
        reader.fetch_post.assert_not_called()
        self.assertTrue(any(a['kind']=='comment' for a in r.state['actions'].values()))
    def test_read_credit_guard_blocks_actor_without_spending_quota(self):
        r=self.runner()
        r.read_budget_guard=lambda: False
        call=Mock()
        self.assertIsNone(r.read(call))
        call.assert_not_called()
        self.assertEqual(r.counts['read'],0)
