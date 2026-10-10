"""Model backend selection and credential-destination contracts, without API calls."""
import json
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import requests

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
    def test_only_local_decoder_receives_strict_schema_and_cannot_match_typographic_dashes(self, post, bridge):
        post.return_value = self.response()
        post.return_value.json.return_value = self.native_text(INTERACTION_TEXT)
        with patch.dict(os.environ, {'MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT,
                                    'APIFY_TOKEN': 'apify-test-placeholder',
                                    'MODEL_API_KEY': 'model-test-placeholder'}, clear=True):
            twin.complete(*twin.model_connection(), self.messages)
        payload = post.call_args.kwargs['json']
        response_format = payload['response_format']
        self.assertEqual(response_format['type'], 'json_schema')
        self.assertTrue(response_format['json_schema']['strict'])
        schema = response_format['json_schema']['schema']
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['required']), {'text', 'skip', 'reason'})
        pattern = schema['properties']['text']['pattern']
        self.assertTrue(re.fullmatch(pattern, INTERACTION_TEXT))
        self.assertTrue(re.fullmatch(pattern, 'An early-stage team can test it.'))
        for dash in ('\u2012', '\u2013', '\u2014', '\u2015'):
            with self.subTest(dash=dash):
                self.assertIsNone(re.fullmatch(pattern, 'Test it ' + dash + ' then decide.'))
        self.assertEqual(post.call_args.kwargs['headers'], {'Authorization': 'Bearer twin-local'})
        bridge.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_remote_provider_keeps_native_json_object_format(self, post, bridge):
        bridge.return_value = self.native_text(INTERACTION_TEXT)
        with patch.dict(os.environ, {'APIFY_TOKEN': 'apify-test-placeholder'}, clear=True):
            twin.complete(*twin.model_connection(), self.messages)
        self.assertEqual(bridge.call_args.args[1]['response_format'], {'type': 'json_object'})
        post.assert_not_called()

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
                    expected_kind = twin.generation_kind(task, context)
                    self.assertEqual(post.call_args.kwargs['json']['max_tokens'],
                                     twin.LOCAL_COMPLETION_TOKENS[expected_kind])
                    self.assertEqual(post.call_args.kwargs['timeout'], (5, 420))
                    self.assertFalse(post.call_args.kwargs['allow_redirects'])
        bridge.assert_not_called()

    @patch('automation.twin.requests.post')
    def test_cpu_limits_short_interactions_without_changing_remote_token_budget(self, post):
        post.return_value = self.response()
        for kind, cap in (('comment', 256), ('reply', 224), ('dm', 400),
                          ('post', 600), ('analysis', 700), ('generic', 900)):
            with self.subTest(kind=kind):
                twin.complete(twin.LOCAL_MODEL_ENDPOINT, 'twin-local', 'twin-local', True,
                              self.messages, kind=kind)
                self.assertEqual(post.call_args.kwargs['json']['max_tokens'], cap)
                self.assertEqual(post.call_args.kwargs['timeout'], (5, 420))
                twin.complete(twin.OPENAI_MODEL_ENDPOINT, 'model-test-placeholder', 'gpt-4.1-mini', False,
                              self.messages, kind=kind)
                self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 900)
                self.assertEqual(post.call_args.kwargs['timeout'], 90)

    @patch('automation.twin.requests.post')
    def test_cpu_timeout_is_distinct_from_server_unavailability_and_never_retries(self, post):
        for error in (requests.ReadTimeout('sensitive-provider-diagnostic'),
                      requests.ConnectTimeout('sensitive-provider-diagnostic')):
            with self.subTest(error=type(error).__name__):
                post.reset_mock()
                post.side_effect = error
                with self.assertRaisesRegex(twin.LocalModelError, 'generation timed out') as caught:
                    twin.complete(twin.LOCAL_MODEL_ENDPOINT, 'twin-local', 'twin-local', True,
                                  self.messages, kind='comment')
                self.assertNotIn('sensitive-provider-diagnostic', str(caught.exception))
                post.assert_called_once()

    @patch('automation.twin.requests.post')
    def test_cpu_prompt_preserves_rules_once_and_all_source_data(self, post):
        post.return_value = self.response()
        post.return_value.json.return_value = self.native_text(INTERACTION_TEXT)
        task = twin.skills('linkedin-comment-drafter', 'linkedin-humanizer', 'linkedin-thread-monitor')
        context = {'post': {'text': 'Compare prototypes on the same customer task.'}}
        with patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT}, clear=True):
            result = twin.model(task, context, POLICY)
        self.assertFalse(result['skip'])
        payload = post.call_args.kwargs['json']
        messages = payload['messages']
        system, user = messages[0]['content'], messages[1]['content']
        self.assertEqual(system.count('FINAL OUTPUT REQUIREMENTS'), 1)
        self.assertIn(twin.voice.prompt_rules(), system)
        self.assertNotIn('FINAL OUTPUT REQUIREMENTS', user)
        self.assertEqual(json.loads(user.split('\n', 1)[1]), context)
        for name in ('linkedin-comment-drafter', 'linkedin-humanizer', 'linkedin-thread-monitor'):
            self.assertIn('Skill: ' + name, system)
        self.assertLess(len(twin.compact_skill_context(task)), 3200)
        self.assertEqual(payload['max_tokens'], 256)

    @patch('automation.twin.complete')
    def test_unsourced_observation_is_recast_once_before_it_can_be_sent(self, complete):
        invented = ('I\u2019ve seen teams use AI sims to test copy changes in customer onboarding flows. '
                    'They often skip validating emotional tone, which leads to high drop-off rates.')
        conditional = ('A simulation could help compare two onboarding messages. I\u2019d still test the chosen '
                       'copy with customers, because a simulated response can miss how the wording feels in context.')
        complete.side_effect = [{'text': invented, 'skip': False}, {'text': conditional, 'skip': False}]
        with patch.dict(os.environ, {'MODEL_API_KEY': 'model-test-placeholder'}, clear=True):
            result = twin.model(twin.skills('linkedin-comment-drafter'),
                                {'post': {'text': invented}}, POLICY)
        self.assertEqual(result['text'], conditional)
        self.assertFalse(result['skip'])
        self.assertEqual(complete.call_count, 2)
        editor = complete.call_args.args[-1][0]['content']
        self.assertIn('without a supplied source note are not facts', editor)
        self.assertIn('conditional possibility', editor)

    @patch('automation.twin.complete')
    def test_editor_cannot_preserve_or_add_a_personal_observation(self, complete):
        invented = ('I have seen teams rely on AI simulations for onboarding copy. '
                    'A customer test would help check how the same words feel in the real flow.')
        complete.return_value = {'text': invented, 'skip': False}
        with patch.dict(os.environ, {'MODEL_API_KEY': 'model-test-placeholder'}, clear=True):
            result = twin.model(twin.skills('linkedin-comment-drafter'), {'post': {'text': invented}}, POLICY)
        self.assertTrue(result['skip'])
        self.assertEqual(result['text'], '')
        self.assertIn('personal experience', result['reason'])
        self.assertEqual(complete.call_count, 2)

    def test_source_notes_authorize_only_matching_experience(self):
        claim = 'I have seen teams compare onboarding copy with a customer test.'
        result = {'text': claim, 'skip': False, 'reason': ''}
        matching = dict(POLICY, source_notes=[claim])
        unrelated = dict(POLICY, source_notes=['I have seen teams redesign a checkout page.'])
        self.assertFalse(twin.validate_grounding(result, matching)['skip'])
        self.assertTrue(twin.validate_grounding(result, unrelated)['skip'])
        self.assertTrue(twin.validate_grounding(result, POLICY)['skip'])

    def test_confirmed_career_disciplines_and_design_opinions_remain_allowed(self):
        allowed = {'text': "I've worked across product design and creative direction. "
                           "I'd compare prototypes on the same customer task.", 'skip': False, 'reason': ''}
        self.assertFalse(twin.validate_grounding(allowed, POLICY)['skip'])
        unconfirmed = dict(allowed, text="I've worked across product design and aerospace engineering.")
        self.assertTrue(twin.validate_grounding(unconfirmed, POLICY)['skip'])
        for claim in ('We observed customers complete onboarding faster.',
                      'Our clients found the new flow easier to use.',
                      'In my experience, simulations miss the important details.',
                      "I've often seen clients skip the customer test.",
                      'Last year I redesigned the onboarding flow for a client.',
                      'We recently advised a team on its checkout experience.'):
            with self.subTest(claim=claim):
                self.assertTrue(twin.validate_grounding({'text': claim, 'skip': False, 'reason': ''}, POLICY)['skip'])

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_failed_cpu_edit_gets_one_fresh_budget_guarded_apify_generation(self, post, bridge):
        invented = ('I have seen teams use an unsupplied simulation story to choose onboarding copy. '
                    'The test clarified which message to use in the flow.')
        post.return_value = self.response()
        post.return_value.json.return_value = self.native_text(invented)
        bridge.return_value = self.native_text(INTERACTION_TEXT)
        context = {'post': {'text': 'Compare onboarding messages on the same customer task.'}}
        with patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT,
                                    'APIFY_TOKEN': 'apify-test-placeholder'}, clear=True):
            result = twin.model(twin.skills('linkedin-comment-drafter', 'linkedin-humanizer'), context, POLICY)
        self.assertFalse(result['skip'])
        self.assertEqual(result['text'], INTERACTION_TEXT)
        self.assertEqual(post.call_count, 2)
        bridge.assert_called_once()
        endpoint, request = bridge.call_args.args
        self.assertEqual(endpoint, self.endpoint)
        self.assertEqual(request['max_tokens'], 900)
        system, user = request['messages'][0]['content'], request['messages'][1]['content']
        self.assertEqual(system.count('FINAL OUTPUT REQUIREMENTS'), 1)
        self.assertIn('No owner firsthand observations were supplied', system)
        self.assertNotIn('unsupplied simulation story', json.dumps(request))
        self.assertEqual(json.loads(user.split('\n', 1)[1]), context)

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_quality_fallback_budget_pause_and_invalid_paid_output_remain_held(self, post, bridge):
        invented = ('I have seen teams compare onboarding copy with a customer test. '
                    'That comparison clarified which message would fit the flow.')
        for budget_paused in (True, False):
            with self.subTest(budget_paused=budget_paused):
                post.reset_mock()
                bridge.reset_mock()
                post.return_value = self.response()
                post.return_value.json.return_value = self.native_text(invented)
                bridge.side_effect = apify_ai.ApifyBudgetError('Budget paused') if budget_paused else None
                bridge.return_value = self.native_text(invented)
                with patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT,
                                            'APIFY_TOKEN': 'apify-test-placeholder'}, clear=True):
                    result = twin.model(twin.skills('linkedin-comment-drafter'),
                                        {'post': {'text': 'Compare onboarding copy on a customer task.'}}, POLICY)
                self.assertTrue(result['skip'])
                self.assertEqual(result['text'], '')
                self.assertEqual(post.call_count, 2)
                bridge.assert_called_once()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_valid_cpu_draft_and_intentional_skip_never_use_paid_fallback(self, post, bridge):
        for intentional_skip in (True, False):
            with self.subTest(intentional_skip=intentional_skip):
                post.reset_mock()
                post.return_value = self.response()
                payload = self.native_text(INTERACTION_TEXT)
                if intentional_skip:
                    payload['choices'][0]['message']['content'] = json.dumps({'text': '', 'skip': True, 'reason': 'Unrelated post'})
                post.return_value.json.return_value = payload
                with patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT,
                                            'APIFY_TOKEN': 'apify-test-placeholder'}, clear=True):
                    result = twin.model(twin.skills('linkedin-comment-drafter'), {'post': {'text': 'Design'}}, POLICY)
                self.assertEqual(result['skip'], intentional_skip)
                post.assert_called_once()
        bridge.assert_not_called()

    @patch('automation.apify_ai.complete')
    @patch('automation.twin.requests.post')
    def test_cpu_quality_fallback_cannot_use_a_separately_billed_model_endpoint(self, post, bridge):
        invented = ('I have seen teams compare onboarding copy with a customer test. '
                    'That comparison clarified which message would fit the flow.')
        post.return_value = self.response()
        post.return_value.json.return_value = self.native_text(invented)
        for endpoint in (twin.OPENAI_MODEL_ENDPOINT, 'https://model.example.test/chat/completions'):
            with self.subTest(endpoint=endpoint):
                post.reset_mock()
                with patch.dict(os.environ, {'INTERACTION_MODEL_ENDPOINT': twin.LOCAL_MODEL_ENDPOINT,
                                            'MODEL_ENDPOINT': endpoint, 'MODEL_API_KEY': 'model-test-placeholder',
                                            'APIFY_TOKEN': 'apify-test-placeholder'}, clear=True):
                    result = twin.model(twin.skills('linkedin-comment-drafter'), {'post': {'text': 'Design'}}, POLICY)
                self.assertTrue(result['skip'])
                self.assertEqual(post.call_count, 2)
                self.assertTrue(all(call.args[0] == twin.LOCAL_MODEL_ENDPOINT for call in post.call_args_list))
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
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 600)
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
        self.assertEqual(post.call_args.kwargs['json']['max_tokens'], 600)
        messages = post.call_args.kwargs['json']['messages']
        self.assertEqual(json.loads(messages[1]['content'])['draft'], source)
        self.assertNotIn('must-not-enter-edit', json.dumps(messages))
        self.assertIn('linkedin-humanizer', messages[0]['content'])
