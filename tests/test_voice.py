"""Writing preferences are enforced before any side effect, without detectors."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from automation import twin, voice
from automation.private_actions import PrivateActions
from lib import backend_selector
from lib.publora_client import PubloraClient


NOW = datetime(2026, 10, 10, 3, 30, tzinfo=timezone.utc)
POLICY = {'timezone': 'Asia/Calcutta', 'platform_id': 'linkedin-owner',
          'profile_url': 'https://www.linkedin.com/in/therupeshkumar/',
          'max_posts_per_day': 2, 'max_interactions_per_day': 40,
          'max_replies_per_day': 1000, 'max_read_calls_per_day': 350,
          'source_notes': [], 'background': ['product design'],
          'dm_replies_enabled': True, 'profile_edits_enabled': True}
COMMENT = ('Test both prototypes on the same customer task. A faster tool helps production, '
           'but the team still needs to see where customers get stuck.')
REPLY = 'I would test the checkout step first. Where are people dropping off?'


class VoiceTests(unittest.TestCase):
    def test_dash_spelling_cannot_bypass_the_gate(self):
        for separator in ('\u2014', '\u2013', '\u2012', '\u2015', '--', '&mdash;',
                          '&ndash;', '&#8212;', '&#x2013;', r'\u2014'):
            with self.subTest(separator=separator):
                with self.assertRaisesRegex(voice.VoiceError, 'prohibited dash'):
                    voice.require_plain_text('Test the prototype ' + separator + ' before shipping.')

    def test_ordinary_punctuation_hyphenated_words_and_real_numbers_stay(self):
        text = 'An early-stage team can test C# in Figma (cost: $4,730 on 14 Feb).'
        self.assertEqual(voice.require_plain_text(text), text)

    def test_canned_language_and_formulaic_contrasts_are_rejected(self):
        cases = ['Great post! ' + COMMENT, 'A strong reminder that ' + COMMENT,
                 "Couldn't agree more. " + COMMENT, 'Well said. ' + COMMENT,
                 'Love this. ' + COMMENT, '100% ' + COMMENT, 'Agreed.', 'Thanks for sharing.',
                 'We need to leverage the design ecosystem.', 'This is a game-changer.',
                 'Leveraging research could streamline the process.',
                 'The next design decision needs a test. What do you think?',
                 "It's not about speed. It's about clarity.",
                 "It's not design. It's trust.", 'This resonates with me. ' + COMMENT,
                 'Let that sink in. ' + COMMENT]
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(voice.violations(text))

    def test_specific_disagreement_and_a_natural_thank_you_remain_possible(self):
        for text in (COMMENT, REPLY, "I don't agree on the prototype step. Testing checkout first could expose the problem sooner.",
                     'Thanks, Tushar. The early decision is which customer problem deserves another test.'):
            with self.subTest(text=text):
                self.assertEqual(voice.violations(text), ())

    def test_stock_value_claims_are_rejected_anywhere_without_banning_technical_context(self):
        for phrase in ('Their true value lies in a clearer process.',
                       'The post highlights a key tension between speed and quality.',
                       'This post highlights a useful idea.', 'This makes a meaningful impact.',
                       'The post raises a question about speed.',
                       'This shows a key tension between speed and quality.',
                       'The real question is how teams make decisions.',
                       'A team needs depth of user value.',
                       'The result is purposeful and impactful.',
                       "It can shape your product's trajectory.",
                       'This shapes your product\u2019s trajectory.',
                       'It is shaping the product\u2019s trajectory.'):
            with self.subTest(phrase=phrase):
                self.assertTrue(voice.violations('Test the checkout step first. ' + phrase))
        self.assertFalse(voice.violations('A user-initiated call-up could reduce cognitive load by keeping the extra information out of the task until it is needed.'))

    def test_source_summary_prefaces_are_rejected_and_prompt_examples_are_hypothetical(self):
        for subject in ('The post', 'This post'):
            for verb in ('raises', 'highlights', 'shows', 'reminds', 'underscores',
                         'reinforces', 'captures', 'points out'):
                with self.subTest(subject=subject, verb=verb):
                    self.assertTrue(voice.violations(subject + ' ' + verb + ' a problem worth testing.'))
        prompt = voice.prompt_rules()
        self.assertIn('hypothetical style examples only', prompt)
        self.assertIn('Do not copy their text', prompt)
        self.assertIn("I'd keep the next action visible", prompt)
        self.assertIn('Speak directly to the person', prompt)

    def test_scaffolding_hashtags_emoji_and_assistant_commentary_are_rejected(self):
        for prefix in ('## Design', '**Design**', '1. Test first', '- Test first',
                       'Hook: Test first', 'CTA: What do you think?', '```text\nTest\n```',
                       'Product design #Design', 'Design \U0001f680',
                       'As an AI, I suggest testing first.', '[Design](https://example.com)'):
            with self.subTest(prefix=prefix):
                self.assertTrue(voice.violations(prefix))

    def test_all_generation_kinds_share_the_gate(self):
        for kind in ('post', 'comment', 'reply', 'dm', 'analysis', 'generic'):
            with self.subTest(kind=kind):
                result = twin.validate_model_output({'text': COMMENT + ' \u2014 Test it.', 'skip': False}, kind)
                self.assertTrue(result['skip'])
                self.assertEqual(result['text'], '')
                self.assertIn('prohibited dash', result['reason'])

    def test_specific_brief_replies_are_accepted_without_padding(self):
        result = twin.validate_model_output({'text': REPLY, 'skip': False}, 'reply')
        self.assertFalse(result['skip'])
        self.assertEqual(result['text'], REPLY)
        self.assertLess(len(REPLY), 150)
        self.assertTrue(twin.validate_model_output({'text': 'Agreed.', 'skip': False}, 'reply')['skip'])

    def test_concise_comment_and_short_post_are_accepted(self):
        comment = 'Test the checkout step first. It should reveal whether people understand the delivery cost before they pay.'
        self.assertTrue(80 <= len(comment) < 140)
        self.assertFalse(twin.validate_model_output({'text': comment, 'skip': False}, 'comment')['skip'])
        post = ('A prototype can make a product decision easier to test before the team builds it. '
                'Pick the customer task first, then compare how people complete it. ' * 3).strip()
        self.assertTrue(400 <= len(post) < 900)
        self.assertFalse(twin.validate_model_output({'text': post, 'skip': False}, 'post')['skip'])

    @patch('automation.twin.complete')
    def test_one_source_only_repair_includes_explicit_owner_rules(self, complete):
        source = COMMENT.replace('. A faster', ' \u2014 a faster')
        complete.side_effect = [{'text': source, 'skip': False}, {'text': COMMENT, 'skip': False}]
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-placeholder'}, clear=True):
            result = twin.model(twin.skills('linkedin-comment-drafter'),
                                {'post': {'text': 'A real prototype comparison'}, 'private_detail': 'must-not-enter-edit'}, POLICY)
        self.assertEqual(result['text'], COMMENT)
        self.assertEqual(complete.call_count, 2)
        initial = complete.call_args_list[0].args[-1]
        edited = complete.call_args_list[1].args[-1]
        for messages in (initial, edited):
            self.assertIn('Never use em dashes', messages[0]['content'])
            self.assertIn('OWNER WRITING RULES', messages[0]['content'])
        self.assertEqual(json.loads(edited[1]['content'])['draft'], source)
        self.assertNotIn('must-not-enter-edit', json.dumps(edited))

    @patch('automation.twin.complete')
    def test_a_second_bad_output_is_skipped_without_a_third_model_call(self, complete):
        complete.return_value = {'text': COMMENT + ' \u2014 Test again.', 'skip': False}
        with patch.dict(os.environ, {'MODEL_API_KEY': 'test-placeholder'}, clear=True):
            result = twin.model(twin.skills('linkedin-reply-handler'), {'comment': {'text': 'How would you test it?'}}, POLICY)
        self.assertTrue(result['skip'])
        self.assertEqual(result['text'], '')
        self.assertEqual(complete.call_count, 2)

    def test_caller_supplied_draft_is_blocked_before_write_intent_and_any_remote_call(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = twin.Runner(POLICY, Path(temp) / 'state.json', live=True, now=NOW)
            runner.save = Mock()
            runner.post_capacity = Mock()
            with patch('automation.twin.requests.post') as send:
                for kind in ('post', 'comment', 'reply'):
                    with self.subTest(kind=kind), self.assertRaises(voice.VoiceError):
                        runner.write('bad:' + kind, kind, COMMENT + ' \u2014 Test it.', 'urn:li:share:123')
                send.assert_not_called()
            runner.post_capacity.assert_not_called()
            runner.save.assert_not_called()
            self.assertEqual(runner.state['actions'], {})
            self.assertEqual(runner.counts['post'], 0)
            self.assertEqual(runner.counts['interaction'], 0)

    def test_custom_generator_cannot_bypass_draft_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = twin.Runner(POLICY, Path(temp) / 'state.json', now=NOW,
                                 generate=lambda *args: {'text': COMMENT + ' \u2014 Test it.'})
            self.assertIsNone(runner.draft(twin.skills('linkedin-comment-drafter'), {'post': {}}))
            self.assertEqual(runner.state['actions'], {})

    def test_low_level_publora_text_writes_share_gate(self):
        client = PubloraClient(api_key='test-placeholder')
        client._post = Mock()
        bad = COMMENT + ' \u2014 Test it.'
        for operation in (lambda: client.create_post(content=bad, platforms=['linkedin-owner']),
                          lambda: client.create_comment(post_urn='urn:li:share:123', message=bad, platform_id='linkedin-owner'),
                          lambda: client.create_reshare(parent='urn:li:share:123', platform_id='linkedin-owner', commentary=bad)):
            with self.assertRaises(voice.VoiceError):
                operation()
        client._post.assert_not_called()

    def test_publish_dispatch_blocks_bad_copy_before_reactions_or_backend_execution(self):
        for backend in ('publora', 'diy', 'manual'):
            with self.subTest(backend=backend), patch.object(backend_selector, 'active_backend', return_value=backend), \
                 patch('lib.backend_selector.subprocess.run') as execute, \
                 patch('lib.publora_client.PubloraClient') as client:
                with self.assertRaises(voice.VoiceError):
                    backend_selector.publish('comment', COMMENT + ' \u2014 Test it.', 'https://linkedin.com/post',
                                             post_urn='urn:li:share:123', reaction_type='LIKE')
                execute.assert_not_called()
                client.assert_not_called()

    def test_reshare_blocks_bad_commentary_before_paid_reads(self):
        with patch.object(backend_selector, 'fetch_post') as read:
            with self.assertRaises(voice.VoiceError):
                backend_selector.repost('https://linkedin.com/post', commentary=COMMENT + ' \u2014 Test it.')
            read.assert_not_called()

    def test_private_prepared_profile_is_blocked_before_intent(self):
        state = {}
        client = Mock()
        client.verify_account.return_value = {'provider': 'LINKEDIN', 'public_identifier': 'therupeshkumar'}
        save = Mock()
        private = PrivateActions(state, save, client, NOW, POLICY, Mock(), live=True)
        with self.assertRaises(voice.VoiceError):
            private.apply_profile({'headline': 'Product designer', 'summary': COMMENT + ' \u2014 Test it.'})
        save.assert_not_called()
        client.update_profile.assert_not_called()
        self.assertNotIn('private_actions', state)

    def test_private_custom_draft_is_blocked_before_intent(self):
        state = {}
        client = Mock()
        client.verify_account.return_value = {'provider': 'LINKEDIN', 'public_identifier': 'therupeshkumar'}
        client.list_chats.return_value = {'items': [{'id': 'chat', 'unread_count': 1, 'read_only': 0}], 'cursor': None}
        client.list_messages.return_value = {'items': [{'id': 'message', 'is_sender': 0,
            'timestamp': '2026-10-10T03:29:00Z', 'text': 'Can we discuss the checkout screen?'}], 'cursor': None}
        save = Mock()
        private = PrivateActions(state, save, client, NOW, POLICY,
                                 lambda *args: COMMENT + ' \u2014 Test it.', live=True)
        self.assertEqual(private.run_dm_replies(), 0)
        save.assert_not_called()
        client.reply_in_chat.assert_not_called()
        self.assertFalse(state.get('private_actions'))
        self.assertEqual(state['private_days']['2026-10-10']['dm_reply'], 0)


if __name__ == '__main__':
    unittest.main()
