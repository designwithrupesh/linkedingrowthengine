"""Model backend selection and credential-destination contracts, without API calls."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from automation import apify_ai, twin


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
