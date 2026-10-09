import copy
from datetime import datetime, timezone
import json
import unittest

from automation.private_actions import (PrivateActions, PrivateWriteCheckpointError,
                                        PrivateWriteOutcomeError, digest)
from automation.unipile_client import UnipileWriteError


NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
POLICY = {'timezone': 'Asia/Calcutta', 'profile_url': 'https://www.linkedin.com/in/therupeshkumar/',
          'dm_replies_enabled': True, 'profile_edits_enabled': True}
PLAN = {'headline': 'Product designer for early-stage founders',
        'summary': 'I work across product design, creative direction and brand strategy.'}


def message(identifier='private-message-id', *, text='Can we discuss a design problem?', own=False,
            stamp='2026-10-09T09:59:00Z', **extra):
    return {'id': identifier, 'text': text, 'is_sender': int(own), 'timestamp': stamp, **extra}


class Client:
    def __init__(self):
        self.verified = []
        self.chats = [{'id': 'private-chat-id', 'unread_count': 1, 'read_only': 0}]
        self.messages = {'private-chat-id': [message()]}
        self.dm_writes = []
        self.profile_writes = []
        self.reads = []
        self.error = None
        self.reply_ack = {'object': 'MessageSent', 'message_id': 'private-outbound-id'}
        self.profile_ack = {'object': 'ProfileEdited'}

    def verify_account(self, expected):
        self.verified.append(expected)
        return {'provider': 'LINKEDIN', 'public_identifier': expected, 'headline': PLAN['headline']}

    def list_chats(self, **kwargs):
        self.reads.append(('chats', kwargs))
        return {'items': self.chats, 'cursor': None}

    def list_messages(self, chat_id, **kwargs):
        self.reads.append(('messages', chat_id, kwargs))
        return {'items': self.messages[chat_id], 'cursor': None}

    def reply_in_chat(self, chat_id, text):
        self.dm_writes.append((chat_id, text))
        if self.error:
            raise self.error
        return self.reply_ack

    def update_profile(self, headline, summary):
        self.profile_writes.append((headline, summary))
        if self.error:
            raise self.error
        return self.profile_ack


class PrivateActionsTests(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.state = {}
        self.saved = []
        self.drafts = []

    def save(self):
        self.saved.append(copy.deepcopy(self.state))

    def draft(self, task, context):
        self.drafts.append((task, context))
        return 'What is the main product decision your team is working through?'

    def runner(self, **kwargs):
        values = {'state': self.state, 'save': self.save, 'client': self.client, 'now': NOW,
                  'policy': POLICY, 'draft': self.draft, 'live': True}
        values.update(kwargs)
        return PrivateActions(**values)

    def test_missing_binding_and_preview_make_no_private_calls(self):
        self.assertEqual(self.runner(client=None).run_dm_replies(), 0)
        self.assertFalse(self.runner(client=None).apply_profile(PLAN))
        self.assertEqual(self.runner(live=False).run_dm_replies(), 0)
        self.assertFalse(self.runner(live=False).apply_profile(PLAN))
        self.assertEqual(self.client.verified, [])
        self.assertEqual(self.state, {})

    def test_private_capabilities_require_explicit_policy_flags(self):
        policy = {key: value for key, value in POLICY.items() if not key.endswith('_enabled')}
        self.assertEqual(self.runner(policy=policy).run_dm_replies(), 0)
        self.assertFalse(self.runner(policy=policy).apply_profile(PLAN))
        self.assertEqual(self.client.verified, [])

    def test_verified_owner_must_match_configured_linkedin_profile(self):
        self.client.verify_account = lambda expected: {'provider': 'LINKEDIN', 'public_identifier': 'someoneelse'}
        with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
            self.runner().run_dm_replies()
        self.assertEqual(self.client.reads, [])

    def test_dm_receipts_never_store_private_ids_text_names_or_drafts(self):
        self.client.chats[0]['name'] = 'Private Customer Name'
        self.client.messages['private-chat-id'][0]['text'] = 'Our unreleased product is called Private Project.'
        self.assertEqual(self.runner().run_dm_replies(), 1)
        self.assertEqual(self.client.verified, ['therupeshkumar'])
        serialized = json.dumps(self.state)
        for private in ('private-chat-id', 'private-message-id', 'private-outbound-id',
                        'Private Customer Name', 'Private Project', self.client.dm_writes[0][1]):
            self.assertNotIn(private, serialized)
        action = next(iter(self.state['private_actions'].values()))
        self.assertEqual(action['status'], 'sent')
        self.assertEqual(action['remote_id_sha256'], digest('private-outbound-id'))
        self.assertEqual(self.saved[0]['private_actions'][next(iter(self.state['private_actions']))]['status'], 'inflight')
        self.assertEqual(self.saved[0]['private_days']['2026-10-09']['dm_reply'], 1)

    def test_duplicate_unread_message_is_never_answered_twice(self):
        self.assertEqual(self.runner().run_dm_replies(), 1)
        self.assertEqual(self.runner().run_dm_replies(), 0)
        self.assertEqual(len(self.client.dm_writes), 1)
        self.assertEqual(len(self.drafts), 1)

    def test_owner_latest_message_prevents_reply_to_older_inbound(self):
        self.client.messages['private-chat-id'] = [message(), message('owner-message', own=True, stamp='2026-10-09T10:00:00Z')]
        self.assertEqual(self.runner().run_dm_replies(), 0)
        self.assertEqual(self.drafts, [])

    def test_latest_inbound_is_selected_by_timestamp_not_response_order(self):
        self.client.messages['private-chat-id'] = [message('owner-message', own=True, stamp='2026-10-09T09:00:00Z'),
                                                   message(text='Newest relevant question.')]
        self.assertEqual(self.runner().run_dm_replies(), 1)
        self.assertEqual(self.drafts[0][1]['inbound_message'], 'Newest relevant question.')

    def test_unknown_or_tied_message_order_defers_reply(self):
        for messages in ([message(stamp=None)], [message(), message('other-id')]):
            self.client.messages['private-chat-id'] = messages
            self.assertEqual(self.runner().run_dm_replies(), 0)
        self.assertEqual(self.client.dm_writes, [])

    def test_credentials_in_inbound_or_draft_are_never_sent_to_model_or_linkedin(self):
        self.client.messages['private-chat-id'] = [message(text='api_key: sk-abcdefghijklmnopqrstuv')]
        self.assertEqual(self.runner().run_dm_replies(), 0)
        self.assertEqual(self.drafts, [])
        self.client.messages['private-chat-id'] = [message()]
        self.assertEqual(self.runner(draft=lambda *_: 'password: confidential').run_dm_replies(), 0)
        self.assertEqual(self.client.dm_writes, [])

    def test_model_budget_failure_does_not_create_write_intent(self):
        def exhausted(*args):
            raise RuntimeError('Model budget reserve reached')
        with self.assertRaisesRegex(RuntimeError, 'budget reserve'):
            self.runner(draft=exhausted).run_dm_replies()
        self.assertEqual(self.state.get('private_actions'), {})
        self.assertEqual(self.client.dm_writes, [])
        self.assertEqual(self.saved, [])

    def test_failed_prewrite_checkpoint_prevents_remote_operation(self):
        def failed():
            raise RuntimeError('Checkpoint unavailable')
        with self.assertRaisesRegex(RuntimeError, 'Checkpoint unavailable'):
            self.runner(save=failed).run_dm_replies()
        self.assertEqual(self.client.dm_writes, [])
        self.assertEqual(next(iter(self.state['private_actions'].values()))['status'], 'inflight')

    def test_uncertain_write_is_saved_and_quarantines_same_conversation(self):
        self.client.error = UnipileWriteError('unsafe provider body', uncertain=True, http_status=503)
        with self.assertRaises(PrivateWriteOutcomeError) as raised:
            self.runner().run_dm_replies()
        self.assertNotIn('unsafe provider body', str(raised.exception))
        self.client.error = None
        self.client.messages['private-chat-id'] = [message('new-inbound-id')]
        self.assertEqual(self.runner().run_dm_replies(), 0)
        self.assertEqual(len(self.client.dm_writes), 1)
        self.assertEqual(next(iter(self.state['private_actions'].values()))['status'], 'unknown-needs-reconciliation')

    def test_definitive_rejection_is_recorded_and_never_retried(self):
        self.client.error = UnipileWriteError('unsafe provider body', uncertain=False, http_status=403)
        self.assertEqual(self.runner().run_dm_replies(), 0)
        self.assertEqual(self.runner().run_dm_replies(), 0)
        action = next(iter(self.state['private_actions'].values()))
        self.assertEqual(action['status'], 'rejected')
        self.assertEqual(action['http_status'], 403)
        self.assertEqual(len(self.client.dm_writes), 1)
        self.assertNotIn('unsafe provider body', json.dumps(self.state))

    def test_invalid_acknowledgement_is_quarantined_without_retry(self):
        self.client.reply_ack = {'object': 'MessageSent'}
        with self.assertRaises(PrivateWriteOutcomeError):
            self.runner().run_dm_replies()
        self.assertEqual(self.runner().run_dm_replies(), 0)
        self.assertEqual(len(self.client.dm_writes), 1)

    def test_acknowledged_write_keeps_success_if_receipt_checkpoint_fails(self):
        calls = []
        def checkpoint():
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError('Checkpoint unavailable')
        with self.assertRaises(PrivateWriteCheckpointError):
            self.runner(save=checkpoint).run_dm_replies()
        self.assertEqual(next(iter(self.state['private_actions'].values()))['status'], 'sent')
        self.assertEqual(self.runner().run_dm_replies(), 0)
        self.assertEqual(len(self.client.dm_writes), 1)

    def test_daily_and_run_caps_count_one_attempt_each(self):
        self.client.chats = [{'id': f'chat-{n}', 'unread_count': 1} for n in range(4)]
        self.client.messages = {f'chat-{n}': [message(f'inbound-{n}')] for n in range(4)}
        policy = {**POLICY, 'max_dm_replies_per_run': 2, 'max_dm_replies_per_day': 3}
        self.assertEqual(self.runner(policy=policy).run_dm_replies(), 2)
        self.assertEqual(self.runner(policy=policy).run_dm_replies(), 1)
        self.assertEqual(self.runner(policy=policy).run_dm_replies(), 0)
        self.assertEqual(len(self.client.dm_writes), 3)

    def test_zero_quota_disables_dm_operations(self):
        self.assertEqual(self.runner().run_dm_replies(max_messages=0), 0)
        self.assertEqual(self.runner(policy={**POLICY, 'max_dm_replies_per_day': 0}).run_dm_replies(), 0)
        self.assertEqual(self.client.verified, [])

    def test_pagination_collects_newest_inbound_across_bounded_pages(self):
        def pages(chat_id, *, cursor, **kwargs):
            return ({'items': [message('old-owner', own=True, stamp='2026-10-09T09:00:00Z')], 'cursor': 'second'}
                    if cursor is None else {'items': [message()], 'cursor': None})
        self.client.list_messages = pages
        self.assertEqual(self.runner().run_dm_replies(), 1)

    def test_incomplete_paginated_history_and_repeating_cursor_are_deferred(self):
        self.client.list_messages = lambda *args, **kwargs: {'items': [message()], 'cursor': 'more'}
        self.assertEqual(self.runner().run_dm_replies(), 0)
        self.assertEqual(self.client.dm_writes, [])

    def test_profile_plan_is_applied_once_without_storing_profile_text(self):
        self.assertTrue(self.runner().apply_profile(PLAN))
        self.assertFalse(self.runner().apply_profile(PLAN))
        self.assertEqual(self.client.profile_writes, [(PLAN['headline'], PLAN['summary'])])
        self.assertNotIn(PLAN['headline'], json.dumps(self.state))
        self.assertNotIn(PLAN['summary'], json.dumps(self.state))
        self.assertEqual(self.state['private_days']['2026-10-09']['profile_edit'], 1)

    def test_uncertain_profile_update_blocks_even_a_changed_plan(self):
        self.client.error = UnipileWriteError('unsafe profile body', uncertain=True)
        with self.assertRaises(PrivateWriteOutcomeError):
            self.runner().apply_profile(PLAN)
        self.client.error = None
        self.assertFalse(self.runner().apply_profile({**PLAN, 'headline': 'Changed prepared headline'}))
        self.assertEqual(len(self.client.profile_writes), 1)

    def test_invalid_or_sensitive_profile_plan_cannot_create_intent(self):
        for plan in ({'headline': 'Only a headline'}, {**PLAN, 'summary': 'api key: confidential'}):
            with self.assertRaises(ValueError):
                self.runner().apply_profile(plan)
        self.assertEqual(self.client.profile_writes, [])
        self.assertEqual(self.state.get('private_actions', {}), {})

    def test_digest_uses_structural_encoding(self):
        self.assertNotEqual(digest('ab', 'c'), digest('a', 'bc'))


if __name__ == '__main__':
    unittest.main()
