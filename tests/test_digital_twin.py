import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from datetime import datetime, timezone
from automation.twin import Runner, flatten_comments, preflight, model

POLICY = json.loads(Path('automation/policy.json').read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 13, 3, 30, tzinfo=timezone.utc)
TEXT = 'A concrete design observation.'

class DigitalTwinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.json'
    def tearDown(self):
        self.temp.cleanup()
    def runner(self, **kw):
        return Runner(POLICY, self.path, now=NOW, generate=lambda *a: {'text': TEXT}, **kw)
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
    def test_nested_parent_and_own_comment_filter(self):
        rows = [{'comment_id':'1','author':{'profile_url':POLICY['profile_url']},'replies':[{'comment_id':'2','text':'question','author':{'profile_url':'https://www.linkedin.com/in/other/'}}]}]
        result = flatten_comments(rows, POLICY['profile_url'])
        self.assertEqual([(c['id'],c['parent_id']) for c in result], [('2','1')])
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
    @patch('automation.twin.requests.get')
    def test_missing_secret_stops_before_network(self, get):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                preflight(POLICY)
        get.assert_not_called()
    @patch('automation.twin.requests.get')
    def test_wrong_channel_blocks_preflight(self, get):
        get.return_value.json.return_value = {'connections':[{'platformId':'linkedin-other'}]}
        with patch.dict(os.environ, {'PUBLORA_API_KEY':'test-placeholder','GITHUB_TOKEN':'test-placeholder'}):
            with self.assertRaises(RuntimeError):
                preflight(POLICY)
    @patch('automation.twin.requests.get')
    def test_discover_published_uses_post_id_path_and_saves_permalink(self, get):
        url = 'https://www.linkedin.com/feed/update/urn:li:share:7000000000000000000/'
        get.return_value.json.return_value = {'posts': [{'permalink': url}]}
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
    @patch('automation.twin.requests.post')
    def test_empty_model_variables_use_defaults(self, post):
        post.return_value.json.return_value={'choices':[{'message':{'content':'{"skip":true}'}}]}
        with patch.dict(os.environ, {'MODEL_API_KEY':'test-placeholder','MODEL_ENDPOINT':'','MODEL_NAME':''}):
            model('task', {}, POLICY)
        self.assertEqual(post.call_args.args[0], 'https://models.github.ai/inference/chat/completions')
        self.assertEqual(post.call_args.kwargs['json']['model'], 'openai/gpt-4.1')

    @patch('automation.twin.requests.post')
    def test_compatible_model_fenced_json(self, post):
        post.return_value.json.return_value={'choices':[{'message':{'content':'```json\n{"text":"draft","skip":false}\n```'}, 'finish_reason':'stop'}]}
        with patch.dict(os.environ, {'MODEL_API_KEY':'test-placeholder'}):
            result = model('task', {}, POLICY)
        self.assertEqual(result['text'], 'draft')
