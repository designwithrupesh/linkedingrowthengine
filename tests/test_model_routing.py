"""Model backend selection and credential-destination contracts, without API calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from automation import apify_ai, twin


POLICY = {
    'timezone': 'Asia/Calcutta',
    'background': ['creative direction', 'product design'],
    'goals': ['consulting clients', 'founding designer roles'],
    'audience': 'early-stage founders and product leaders',
    'boundaries': 'Do not invent clients, outcomes, prices or commitments.',
    'authorization': 'The owner authorized routine contextual replies and posts.',
    'source_notes': [],
    'platform_id': 'public-platform-identifier',
    'private_configuration': 'configuration-must-not-enter-a-model-prompt',
}
INTERACTION_TEXT = ('An early team could compare two prototypes on the same customer task. '
                    'If a tool speeds up production but leaves the same confusion in the experience, '
                    'the gain is mostly in throughput. The stronger signal is a clearer decision about what to build.')
POST_TEXT = ('Research should inform the next design decision. A prototype can expose an uncertain '
             'assumption before a team commits to the full product. ' * 8).strip()


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / 'ai-provider.json'
        self.config.write_text(json.dumps({'actor_id': 'Actor123456789ABC', 'build_id': 'Build123456789ABC',
                                          'build_number': '0.1.2', 'model': apify_ai.MODEL,
                                          'name': 'linkedin-growth-ai'}), encoding='utf-8')
        self.config_patch = patch.object(apify_ai, 'CONFIG_PATH', self.config)
        self.config_patch.start()
        self.endpoint = apify_ai.model_info()[0]
        self.native = {'choices': [{'message': {'content': '{"text":"draft","skip":false}'}, 'finish_reason': 'stop'}]}
        self.messages = [{'role': 'user', 'content': 'A public design observation'}]

    def tearDown(self):
        self.config_patch.stop()
        self.temp.cleanup()

    def response(self):
        response = Mock(status_code=200, content=b'{}')
        response.json.return_value = self.native
        return response

    def native_text(self, text):
        return {'choices': [{'message': {'content': json.dumps({'text': text, 'skip': False, 'reason': ''})},
                             'finish_reason': 'stop'}]}

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_explicit_model_key_prefers_openai_and_never_uses_apify_binding(self, post, bridge):
        post.return_value = self.response()
        with patch.dict(os.environ, {'MODEL_API_KEY': 'model-test-placeholder', 'APIFY_TOKEN': 'apify-test-placeholder',
                                    'GITHUB_TOKEN': 'github-test-placeholder'}, clear=True):
            connection = twin.model_connection()
            self.assertEqual(connection, (twin.OPENAI_MODEL_ENDPOINT, 'model-test-placeholder', 'gpt-4.1-mini', False))
            self.assertEqual(twin.complete(*connection, self.messages)['text'], 'draft')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer model-test-placeholder')
        bridge.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_default_apify_binding_prefers_pinned_bridge_over_github(self, post, bridge):
        bridge.return_value = self.native
        with patch.dict(os.environ, {'APIFY_TOKEN': 'apify-test-placeholder', 'GITHUB_TOKEN': 'github-test-placeholder'}, clear=True):
            connection = twin.model_connection()
            self.assertEqual(connection[0], self.endpoint)
            self.assertEqual(connection[2], apify_ai.MODEL)
            self.assertFalse(connection[3])
            self.assertEqual(twin.complete(*connection, self.messages)['text'], 'draft')
        bridge.assert_called_once()
        self.assertEqual(bridge.call_args.args[0], self.endpoint)
        self.assertEqual(bridge.call_args.args[1]['model'], apify_ai.MODEL)
        self.assertEqual(bridge.call_args.args[1]['messages'], self.messages)
        post.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_stale_model_name_cannot_override_build_pinned_bridge_model(self, post, bridge):
        bridge.return_value = self.native
        with patch.dict(os.environ, {'APIFY_TOKEN': 'apify-test-placeholder', 'MODEL_NAME': 'twin-local'}, clear=True):
            connection = twin.model_connection()
            self.assertEqual(connection[0], self.endpoint)
            self.assertEqual(connection[2], apify_ai.MODEL)
            twin.complete(*connection, self.messages)
        self.assertEqual(bridge.call_args.args[1]['model'], 'openai/gpt-4.1-mini')
        bridge.assert_called_once()
        post.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_explicit_loopback_overrides_automatic_bridge_without_forwarding_tokens(self, post, bridge):
        post.return_value = self.response()
        with patch.dict(os.environ, {'MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT, 'APIFY_TOKEN': 'apify-test-placeholder',
                                    'MODEL_API_KEY': 'model-test-placeholder', 'GITHUB_TOKEN': 'github-test-placeholder'}, clear=True):
            connection = twin.model_connection()
            self.assertEqual(connection, (twin.LOCAL_MODEL_ENDPOINT, 'twin-local', 'twin-local', True))
            self.assertEqual(twin.complete(*connection, self.messages)['text'], 'draft')
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer twin-local')
        bridge.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_spoofed_custom_endpoint_cannot_borrow_apify_or_github_credentials(self, post, bridge):
        for endpoint in (self.endpoint.replace('api.apify.com', 'api.apify.com.evil.example'),
                         self.endpoint.replace('/run-sync?', '/run-sync-get-dataset-items?'),
                         self.endpoint + '&token=unexpected',
                         self.endpoint.replace('Actor123456789ABC', 'Other123456789ABC'),
                         self.endpoint.replace('build=0.1.2', 'build=0.1.3'),
                         'https://models.github.ai.evil.example/inference/chat/completions'):
            with self.subTest(endpoint=endpoint), \
                 patch.dict(os.environ, {'MODEL_ENDPOINT': endpoint, 'APIFY_TOKEN': 'apify-test-placeholder',
                                         'GITHUB_TOKEN': 'github-test-placeholder'}, clear=True):
                with self.assertRaises(twin.ModelConfigurationError):
                    twin.model_connection()
        post.assert_not_called()
        bridge.assert_not_called()

    def test_invalid_metadata_falls_back_to_official_github_without_using_apify_token(self):
        self.config.write_text('{}', encoding='utf-8')
        with patch.dict(os.environ, {'APIFY_TOKEN': 'apify-test-placeholder', 'GITHUB_TOKEN': 'github-test-placeholder'}, clear=True):
            self.assertEqual(twin.model_connection(),
                             (twin.GITHUB_MODEL_ENDPOINT, 'github-test-placeholder', 'openai/gpt-4.1', False))

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_explicit_official_github_endpoint_keeps_its_own_credential_route(self, post, bridge):
        post.return_value = self.response()
        with patch.dict(os.environ, {'MODEL_ENDPOINT': twin.GITHUB_MODEL_ENDPOINT, 'APIFY_TOKEN': 'apify-test-placeholder',
                                    'GITHUB_TOKEN': 'github-test-placeholder'}, clear=True):
            connection = twin.model_connection()
            self.assertEqual(connection[0], twin.GITHUB_MODEL_ENDPOINT)
            twin.complete(*connection, self.messages)
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer github-test-placeholder')
        bridge.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_hybrid_comments_replies_and_dms_use_only_dummy_loopback_authorization(self, post, bridge):
        post.return_value = self.response()
        post.return_value.json.return_value = self.native_text(INTERACTION_TEXT)
        cases = [
            (twin.skills('linkedin-comment-drafter'), {'post': {'text': 'A concrete product trade-off.'}}),
            (twin.skills('linkedin-reply-handler'), {'comment': {'text': 'What would you test first?'}}),
            (twin.skills('linkedin-reply-handler'), {'channel': 'private_inbox', 'message': 'Can we discuss design?'}),
        ]
        with patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT,
                                    'MODEL_ENDPOINT': self.endpoint, 'MODEL_NAME': apify_ai.MODEL,
                                    'MODEL_API_KEY': 'model-test-placeholder', 'APIFY_TOKEN': 'apify-test-placeholder',
                                    'GITHUB_TOKEN': 'github-test-placeholder'}, clear=True):
            for task, context in cases:
                with self.subTest(context=context):
                    post.reset_mock()
                    result = twin.model(task, context, POLICY)
                    self.assertEqual(result['text'], INTERACTION_TEXT)
                    self.assertFalse(result['skip'])
                    post.assert_called_once()
                    self.assertEqual(post.call_args.args[0], twin.LOCAL_MODEL_ENDPOINT)
                    self.assertEqual(post.call_args.kwargs['headers'], {'Authorization': 'Bearer twin-local'})
                    self.assertEqual(post.call_args.kwargs['json']['model'], 'twin-local')
                    self.assertEqual(post.call_args.kwargs['timeout'], 180)
                    self.assertFalse(post.call_args.kwargs['allow_redirects'])
        bridge.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_hybrid_posts_keep_the_primary_paid_bridge_and_only_supplied_facts(self, post, bridge):
        bridge.return_value = self.native_text(POST_TEXT)
        with patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT,
                                    'APIFY_TOKEN': 'apify-test-placeholder'}, clear=True):
            result = twin.model(twin.skills('linkedin-post-writer'), {'topic': 'Product design decisions'}, POLICY)
        self.assertEqual(result['text'], POST_TEXT)
        self.assertFalse(result['skip'])
        bridge.assert_called_once()
        self.assertEqual(bridge.call_args.args[0], self.endpoint)
        payload = bridge.call_args.args[1]
        self.assertEqual(payload['model'], apify_ai.MODEL)
        system = payload['messages'][0]['content']
        self.assertIn('creative direction', system)
        self.assertIn('consulting clients', system)
        self.assertNotIn(POLICY['platform_id'], system)
        self.assertNotIn(POLICY['private_configuration'], system)
        post.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_invalid_interaction_destination_rejects_before_any_inference(self, post, bridge):
        endpoints = ['http://localhost:8080/v1/chat/completions',
                     twin.LOCAL_MODEL_ENDPOINT + '?token=unexpected',
                     twin.LOCAL_MODEL_ENDPOINT + '/',
                     'https://api.openai.com/v1/chat/completions',
                     'https://127.0.0.1.evil.example/v1/chat/completions']
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint), \
                 patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': endpoint,
                                         'MODEL_API_KEY': 'model-test-placeholder',
                                         'APIFY_TOKEN': 'apify-test-placeholder'}, clear=True):
                with self.assertRaises(twin.ModelConfigurationError):
                    twin.model(twin.skills('linkedin-comment-drafter'), {'post': {'text': 'Design'}}, POLICY)
        post.assert_not_called()
        bridge.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_pre_inference_budget_pause_falls_back_locally_without_pausing_the_runner(self, post, bridge):
        bridge.side_effect = apify_ai.ApifyBudgetError('Configured budget is unavailable')
        post.return_value = self.response()
        post.return_value.json.return_value = self.native_text(POST_TEXT)
        runner = twin.Runner(POLICY, Path(self.temp.name) / 'state.json')
        with patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT,
                                    'APIFY_TOKEN': 'apify-test-placeholder'}, clear=True), \
             patch('builtins.print') as log:
            result = runner.draft(twin.skills('linkedin-post-writer'), {'topic': 'A product design trade-off'})
        self.assertEqual(result, POST_TEXT)
        self.assertFalse(runner.model_paused)
        self.assertNotEqual(runner.state.get('operational_status', {}).get('ai'), 'budget-paused')
        bridge.assert_called_once()
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], twin.LOCAL_MODEL_ENDPOINT)
        self.assertEqual(post.call_args.kwargs['headers'], {'Authorization': 'Bearer twin-local'})
        self.assertIn('Apify AI budget paused; using the verified free local model.',
                      [call.args[0] for call in log.call_args_list if call.args])

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_provider_error_after_inference_does_not_trigger_a_second_backend(self, post, bridge):
        bridge.side_effect = apify_ai.ApifyModelError('Apify AI request failed (HTTP 500)')
        with patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT,
                                    'APIFY_TOKEN': 'apify-test-placeholder'}, clear=True):
            with self.assertRaises(apify_ai.ApifyModelError):
                twin.model(twin.skills('linkedin-post-writer'), {'topic': 'A design decision'}, POLICY)
        bridge.assert_called_once()
        post.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_budget_fallback_during_edit_keeps_source_only_context_and_runner_active(self, post, bridge):
        source = 'A design decision should clarify what an early team needs to learn. ' * 5
        bridge.side_effect = [self.native_text(source), apify_ai.ApifyBudgetError('Configured budget is unavailable')]
        post.return_value = self.response()
        post.return_value.json.return_value = self.native_text(POST_TEXT)
        runner = twin.Runner(POLICY, Path(self.temp.name) / 'state.json')
        with patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT,
                                    'APIFY_TOKEN': 'apify-test-placeholder'}, clear=True):
            result = runner.draft(twin.skills('linkedin-post-writer'),
                                  {'topic': 'Design decisions', 'unrelated_private_context': 'must-not-enter-edit'})
        self.assertEqual(result, POST_TEXT)
        self.assertFalse(runner.model_paused)
        self.assertEqual(bridge.call_count, 2)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], twin.LOCAL_MODEL_ENDPOINT)
        self.assertEqual(post.call_args.kwargs['headers'], {'Authorization': 'Bearer twin-local'})
        messages = post.call_args.kwargs['json']['messages']
        self.assertEqual(json.loads(messages[1]['content'])['draft'], source)
        self.assertNotIn('must-not-enter-edit', json.dumps(messages))
        self.assertIn('linkedin-humanizer', messages[0]['content'])
