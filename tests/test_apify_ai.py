import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from automation import apify_ai


class ApifyAiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / 'ai-provider.json'
        self.metadata = {'actor_id': 'Actor123456789ABC', 'build_id': 'Build123456789ABC',
                         'build_number': '0.1.2', 'model': apify_ai.MODEL, 'name': 'linkedin-growth-ai'}
        # Apify uses 17-character opaque IDs, never owner~actor aliases.
        self.config.write_text(json.dumps(self.metadata), encoding='utf-8')
        self.endpoint = ('https://api.apify.com/v2/acts/' + self.metadata['actor_id']
                         + '/run-sync?build=' + self.metadata['build_number'] + '&timeout=120&memory=256')

    def tearDown(self):
        self.temp.cleanup()

    def client(self, usage=0.5, cap=5):
        reader = Mock()
        reader.BASE_URL = 'https://api.apify.com/v2'
        reader.token = 'test-apify-placeholder'
        response = reader._session.get.return_value
        response.status_code = 200
        response.json.return_value = {'data': {'current': {'monthlyUsageUsd': usage},
                                             'limits': {'maxMonthlyUsageUsd': cap}}}
        return reader

    def test_metadata_constructs_fixed_model_and_canonical_build_pinned_endpoint(self):
        self.assertEqual(apify_ai.model_info(self.config), (self.endpoint, 'openai/gpt-4.1-mini'))
        self.assertTrue(apify_ai.is_apify_model_endpoint(self.endpoint, self.config))

    def test_absent_or_invalid_metadata_disables_backend(self):
        self.assertIsNone(apify_ai.model_info(Path(self.temp.name) / 'absent.json'))
        for metadata in (None, [], {}, {**self.metadata, 'actor_id': 'owner~actor'},
                         {**self.metadata, 'build_id': '../build'},
                         {**self.metadata, 'build_number': 'latest'},
                         {**self.metadata, 'build_number': '0.1.2&token=value'},
                         {**self.metadata, 'build_number': None},
                         {**self.metadata, 'actor_id': self.metadata['actor_id'] + '?token=value'},
                         {**self.metadata, 'model': 'another/model'},
                         {**self.metadata, 'name': 123}):
            with self.subTest(metadata=metadata):
                self.config.write_text(json.dumps(metadata), encoding='utf-8')
                self.assertIsNone(apify_ai.model_info(self.config))
        self.config.write_text('{not json', encoding='utf-8')
        self.assertIsNone(apify_ai.model_info(self.config))

    @patch('automation.apify_ai.ApifyClient')
    def test_spoofed_destinations_never_get_a_token_or_trigger_budget_reads(self, client):
        endpoints = [self.endpoint.replace('api.apify.com', 'api.apify.com.evil.example'),
                     self.endpoint.replace('https://', 'http://'),
                     self.endpoint.replace('/run-sync?', '/run-sync-get-dataset-items?'),
                     self.endpoint.replace(self.metadata['actor_id'], 'Other123456789ABC'),
                     self.endpoint.replace('build=0.1.2', 'build=0.1.3'),
                     self.endpoint + '&token=unexpected', self.endpoint + '#fragment',
                     self.endpoint.replace('&timeout=120&memory=256', '&memory=256&timeout=120')]
        with patch.dict(os.environ, {'APIFY_TOKEN': 'test-apify-placeholder'}, clear=True):
            for endpoint in endpoints:
                with self.subTest(endpoint=endpoint):
                    self.assertFalse(apify_ai.is_apify_model_endpoint(endpoint, self.config))
                    with self.assertRaises(apify_ai.ApifyModelError):
                        apify_ai.authorization_headers(endpoint, self.config)
        client.assert_not_called()

    @patch('automation.apify_ai.ApifyClient')
    def test_missing_binding_stops_before_budget_or_inference(self, client):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(apify_ai.ApifyModelError, 'secure binding is unavailable'):
                apify_ai.authorization_headers(self.endpoint, self.config)
        client.assert_not_called()

    @patch('automation.apify_ai.requests.post')
    @patch('automation.apify_ai.ApifyClient')
    def test_real_budget_guard_uses_lower_account_cap_and_reserves_free_credit(self, client, post):
        with patch.dict(os.environ, {'APIFY_TOKEN': 'test-apify-placeholder'}, clear=True):
            for usage, cap, allowed in ((0, 5, True), (3.99, 5, True), (4, 5, False),
                                       (2.99, 3, True), (3, 3, False), (4.01, 5, False),
                                       (-1, 5, False), (True, 5, False), (1, None, False),
                                       (float('nan'), 5, False)):
                with self.subTest(usage=usage, cap=cap):
                    reader = self.client(usage, cap)
                    client.return_value = reader
                    if allowed:
                        headers = apify_ai.authorization_headers(self.endpoint, self.config)
                        self.assertEqual(headers['Authorization'], 'Bearer test-apify-placeholder')
                    else:
                        with self.assertRaisesRegex(apify_ai.ApifyBudgetError, 'configured budget or account limits'):
                            apify_ai.authorization_headers(self.endpoint, self.config)
                    reader._session.get.assert_called_once_with(
                        'https://api.apify.com/v2/users/me/limits',
                        headers={'Authorization': 'Bearer test-apify-placeholder'}, timeout=30, allow_redirects=False)
        post.assert_not_called()

    @patch('automation.apify_ai.requests.post')
    @patch('automation.apify_ai.ApifyClient')
    def test_unverifiable_budget_blocks_inference_without_exposing_response(self, client, post):
        reader = self.client()
        reader._session.get.side_effect = requests.ConnectionError('private provider data')
        client.return_value = reader
        with patch.dict(os.environ, {'APIFY_TOKEN': 'test-apify-placeholder'}, clear=True):
            with self.assertRaises(apify_ai.ApifyModelError) as caught:
                apify_ai.complete(self.endpoint, {'model': apify_ai.MODEL, 'messages': []}, config_path=self.config)
        self.assertNotIn('private provider data', str(caught.exception))
        post.assert_not_called()

    @patch('automation.apify_ai.requests.post')
    @patch('automation.apify_ai.ApifyClient')
    def test_native_choices_request_has_one_pinned_post_and_no_dataset_fetch(self, client, post):
        client.return_value = self.client()
        payload = {'model': apify_ai.MODEL, 'messages': [{'role': 'user', 'content': 'A public design observation'}],
                   'max_tokens': 96, 'response_format': {'type': 'json_object'}}
        native = {'choices': [{'message': {'role': 'assistant', 'content': '{"text":"draft","skip":false}'},
                               'finish_reason': 'stop'}]}
        post.return_value.status_code = 200
        post.return_value.json.return_value = native
        with patch.dict(os.environ, {'APIFY_TOKEN': 'test-apify-placeholder'}, clear=True):
            self.assertEqual(apify_ai.complete(self.endpoint, payload, config_path=self.config), native)
        post.assert_called_once_with(self.endpoint,
                                     headers={'Authorization': 'Bearer test-apify-placeholder', 'Content-Type': 'application/json'},
                                     json=payload, timeout=150, allow_redirects=False)
        self.assertNotIn('token=', post.call_args.args[0])
        self.assertEqual(client.return_value._session.get.call_count, 1)

    @patch('automation.apify_ai.requests.post')
    @patch('automation.apify_ai.ApifyClient')
    def test_redirect_and_failure_are_not_retried_or_logged_as_provider_bodies(self, client, post):
        client.return_value = self.client()
        payload = {'model': apify_ai.MODEL, 'messages': []}
        for status in (302, 403, 500):
            with self.subTest(status=status):
                post.reset_mock()
                post.return_value.status_code = status
                post.return_value.json.return_value = {'error': 'private response text'}
                with patch.dict(os.environ, {'APIFY_TOKEN': 'test-apify-placeholder'}, clear=True):
                    with self.assertRaises(apify_ai.ApifyModelError) as caught:
                        apify_ai.complete(self.endpoint, payload, config_path=self.config)
                self.assertIn('HTTP ' + str(status), str(caught.exception))
                self.assertNotIn('private response text', str(caught.exception))
                self.assertEqual(post.call_count, 1)
                post.return_value.json.assert_not_called()

    @patch('automation.apify_ai.requests.post')
    @patch('automation.apify_ai.ApifyClient')
    def test_other_model_never_consumes_budget_or_runs_actor(self, client, post):
        with self.assertRaises(apify_ai.ApifyModelError):
            apify_ai.complete(self.endpoint, {'model': 'unconfigured/model'}, config_path=self.config)
        client.assert_not_called()
        post.assert_not_called()
