import os
import unittest
from unittest.mock import Mock, patch

import requests

from automation.unipile_client import (
    UnipileClient, UnipileConfig, UnipileConfigurationError,
    UnipileReadError, UnipileWriteError,
)


def response(status, payload=None):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


def profile(identifier='therupeshkumar'):
    return {'object': 'AccountOwnerProfile', 'provider': 'LINKEDIN',
            'provider_id': 'owner-provider', 'public_identifier': identifier,
            'headline': 'Product designer', 'email': 'private@example.test'}


def chats(account='owned-account'):
    return {'object': 'ChatList', 'items': [
        {'id': 'known-chat', 'account_id': account, 'account_type': 'LINKEDIN', 'unread_count': 1}
    ], 'cursor': None}


class UnipileConfigTests(unittest.TestCase):
    def test_no_configuration_is_truthfully_disabled_and_partial_fails(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(UnipileClient.from_environment())
        with patch.dict(os.environ, {'UNIPILE_API_KEY': 'test-only-secret'}, clear=True):
            with self.assertRaises(UnipileConfigurationError):
                UnipileClient.from_environment()

    def test_dsn_cannot_send_credentials_to_spoofed_hosts_or_url_fields(self):
        for base in ('http://api1.unipile.com', 'https://api1.unipile.com.evil.test',
                     'https://evil.test/api1.unipile.com', 'https://fakeapi1.unipile.com',
                     'https://api1.unipile.com@evil.test', 'https://name:pass@api1.unipile.com',
                     'https://api1.unipile.com/?key=value', 'https://api1.unipile.com/#key',
                     'https://api1.unipile.com/other', 'https://api1.unipile.com:0',
                     'https://api1.unipile.com:99999', ' https://api1.unipile.com'):
            with self.subTest(base=base), self.assertRaises(UnipileConfigurationError):
                UnipileClient(UnipileConfig(base, 'owned-account', 'test-only-secret'))

    def test_safe_dsn_normalizes_once_and_key_is_redacted_from_repr(self):
        client = UnipileClient(UnipileConfig('https://api12.unipile.com:13111/api/v1/',
                                             'owned-account', 'test-only-secret'))
        self.assertEqual(client.config.base_url, 'https://api12.unipile.com:13111/api/v1')
        self.assertNotIn('test-only-secret', repr(client.config))

    def test_identifier_and_header_injection_are_rejected(self):
        for account, key in [('other/account', 'valid'), ('valid', 'secret\nheader')]:
            with self.assertRaises(UnipileConfigurationError):
                UnipileClient(UnipileConfig('https://api1.unipile.com', account, key))


class UnipileClientTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.client = UnipileClient(UnipileConfig('https://api1.unipile.com:13111',
                                                 'owned-account', 'test-only-secret'),
                                    session=self.session)

    def verify(self):
        self.session.get.return_value = response(200, profile())
        return self.client.verify_account('therupeshkumar')

    def register_chat(self):
        self.verify()
        self.session.get.return_value = response(200, chats())
        self.client.list_chats(unread=True)

    def test_verification_matches_actual_owner_and_does_not_return_email(self):
        result = self.verify()
        self.assertEqual(result['public_identifier'], 'therupeshkumar')
        self.assertNotIn('email', result)
        args, kw = self.session.get.call_args
        self.assertTrue(args[0].endswith('/users/me'))
        self.assertEqual(kw['params'], {'account_id': 'owned-account'})
        self.assertFalse(kw['allow_redirects'])
        self.assertEqual(kw['headers']['X-API-KEY'], 'test-only-secret')

    def test_wrong_owner_or_provider_clears_previous_verification(self):
        self.verify()
        for payload in (profile('somebody-else'), {**profile(), 'provider': 'WHATSAPP'}):
            self.session.get.return_value = response(200, payload)
            with self.assertRaises(UnipileReadError):
                self.client.verify_account('therupeshkumar')
            with self.assertRaises(UnipileConfigurationError):
                self.client.update_profile('Headline', 'About')
        self.session.request.assert_not_called()

    def test_unverified_account_cannot_read_inbox_or_edit_profile(self):
        for operation in (lambda: self.client.list_chats(),
                          lambda: self.client.update_profile('Headline', 'About')):
            with self.assertRaises(UnipileConfigurationError):
                operation()
        self.session.get.assert_not_called()
        self.session.request.assert_not_called()

    def test_chats_and_messages_follow_explicit_cursor_without_cross_account_rows(self):
        self.register_chat()
        self.session.get.return_value = response(200, {
            'object': 'MessageList', 'items': [{'id': 'msg-1', 'account_id': 'owned-account',
                                               'chat_id': 'known-chat', 'text': 'Private',
                                               'is_sender': 0, 'timestamp': '2026-10-09T00:00:00Z'}],
            'cursor': 'next-page'})
        page = self.client.list_messages('known-chat', limit=25, cursor='previous-page')
        self.assertEqual(page['cursor'], 'next-page')
        self.assertEqual(self.session.get.call_args.kwargs['params'],
                         {'limit': 25, 'cursor': 'previous-page'})
        self.assertFalse(self.session.get.call_args.kwargs['allow_redirects'])

    def test_chat_and_message_ownership_are_enforced(self):
        self.verify()
        self.session.get.return_value = response(200, chats('other-account'))
        with self.assertRaises(UnipileReadError):
            self.client.list_chats()
        with self.assertRaises(UnipileConfigurationError):
            self.client.reply_in_chat('known-chat', 'reply')
        self.register_chat()
        for changed in ({'account_id': 'other-account'}, {'chat_id': 'other-chat'}):
            row = {'id': 'message', 'account_id': 'owned-account', 'chat_id': 'known-chat', **changed}
            self.session.get.return_value = response(200, {'object': 'MessageList', 'items': [row], 'cursor': None})
            with self.assertRaises(UnipileReadError):
                self.client.list_messages('known-chat')
        self.session.request.assert_not_called()

    def test_profile_edits_only_prepared_headline_and_about_as_documented_multipart(self):
        self.verify()
        self.session.request.return_value = response(200, {'object': 'ProfileEdited', 'extra': 'ignored'})
        self.assertEqual(self.client.update_profile(' Headline ', ' About '), {'object': 'ProfileEdited'})
        args, kw = self.session.request.call_args
        self.assertEqual(args[0], 'PATCH')
        self.assertTrue(args[1].endswith('/users/me/edit'))
        self.assertEqual(kw['files'], {'type': (None, 'LINKEDIN'),
                         'account_id': (None, 'owned-account'), 'headline': (None, 'Headline'),
                         'summary': (None, 'About')})
        self.assertFalse(kw['allow_redirects'])
        self.assertNotIn('Content-Type', kw['headers'])
        self.session.request.assert_called_once()

    def test_reply_uses_existing_owned_chat_and_requires_message_acknowledgement(self):
        self.register_chat()
        self.session.request.return_value = response(201, {'object': 'MessageSent', 'message_id': 'remote-id'})
        self.assertEqual(self.client.reply_in_chat('known-chat', ' Thanks '),
                         {'object': 'MessageSent', 'message_id': 'remote-id'})
        args, kw = self.session.request.call_args
        self.assertEqual(args[0], 'POST')
        self.assertTrue(args[1].endswith('/chats/known-chat/messages'))
        self.assertEqual(kw['files']['text'], (None, 'Thanks'))
        self.session.request.assert_called_once()

    def test_uncertain_writes_never_retry_or_expose_provider_text(self):
        self.verify()
        failures = [(response(status, {'error': 'private secret payload'}), None)
                    for status in (301, 408, 429, 500, 504)]
        failures += [(response(200, {'object': 'wrong-ack'}), None),
                     (response(200, {'object': 'ProfileEdited'}), requests.Timeout('private secret payload'))]
        for result, exception in failures:
            self.session.request.reset_mock()
            self.session.request.return_value = result
            self.session.request.side_effect = exception
            with self.assertRaises(UnipileWriteError) as raised:
                self.client.update_profile('Headline', 'About')
            self.assertTrue(raised.exception.uncertain)
            self.assertNotIn('private secret payload', str(raised.exception))
            self.session.request.assert_called_once()

    def test_definitive_rejection_is_distinct_from_unknown_or_missing_id(self):
        self.register_chat()
        self.session.request.return_value = response(403, {'error': 'credential secret'})
        with self.assertRaises(UnipileWriteError) as raised:
            self.client.reply_in_chat('known-chat', 'Thanks')
        self.assertFalse(raised.exception.uncertain)
        self.assertEqual(raised.exception.http_status, 403)
        self.session.request.return_value = response(201, {'object': 'MessageSent'})
        with self.assertRaises(UnipileWriteError) as raised:
            self.client.reply_in_chat('known-chat', 'Thanks')
        self.assertTrue(raised.exception.uncertain)

    def test_read_redirect_or_invalid_page_fails_safely(self):
        self.session.get.return_value = response(302, {'error': 'credential secret'})
        with self.assertRaises(UnipileReadError) as raised:
            self.client.verify_account()
        self.assertNotIn('credential secret', str(raised.exception))
        self.register_chat()
        self.session.get.return_value = response(200, {'object': 'MessageList', 'items': [], 'cursor': 99})
        with self.assertRaises(UnipileReadError):
            self.client.list_messages('known-chat')


if __name__ == '__main__':
    unittest.main()
