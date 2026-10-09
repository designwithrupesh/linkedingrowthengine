"""Optional stable Unipile v1 access for the owner's inbox and profile.

Configuration does not connect a LinkedIn account. The owner must finish the
provider's hosted authentication and supply its API key, DSN and account ID.
No cold-outreach endpoint or retrying write is exposed by this adapter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from urllib.parse import urlsplit

import requests


class UnipileConfigurationError(RuntimeError):
    """Missing or unsafe provider configuration; contains no secret values."""


class UnipileReadError(RuntimeError):
    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class UnipileWriteError(RuntimeError):
    """A rejected or uncertain one-attempt write, without provider response text."""

    def __init__(self, message, *, uncertain, http_status=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.http_status = http_status


def _identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value))


def _base_url(value):
    if not isinstance(value, str) or value != value.strip():
        raise UnipileConfigurationError('A valid HTTPS Unipile DSN is required.')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError):
        raise UnipileConfigurationError('A valid HTTPS Unipile DSN is required.') from None
    if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ('', '/', '/api/v1', '/api/v1/')
            or not re.fullmatch(r'api[0-9]*\.unipile\.com', parsed.hostname or '')
            or (port is not None and not 1 <= port <= 65535)):
        raise UnipileConfigurationError('A valid HTTPS Unipile DSN is required.')
    # Rebuild only a verified authority, preserving the provider's assigned port.
    authority = parsed.hostname + (f':{port}' if port is not None else '')
    return f'https://{authority}/api/v1'


@dataclass(frozen=True)
class UnipileConfig:
    base_url: str
    account_id: str
    api_key: str = field(repr=False)

    @classmethod
    def from_environment(cls):
        values = [os.getenv(name, '') for name in
                  ('UNIPILE_BASE_URL', 'UNIPILE_ACCOUNT_ID', 'UNIPILE_API_KEY')]
        if not any(values):
            return None
        base, account_id, key = values
        if not base or not _identifier(account_id) or not key or '\r' in key or '\n' in key:
            raise UnipileConfigurationError('All three Unipile connection settings are required.')
        return cls(_base_url(base), account_id, key)


class UnipileClient:
    def __init__(self, config: UnipileConfig, *, session=None, timeout=30):
        if not isinstance(config, UnipileConfig):
            raise UnipileConfigurationError('Valid Unipile connection settings are required.')
        # Direct construction receives the same checks as environment discovery.
        self.config = UnipileConfig(_base_url(config.base_url), config.account_id, config.api_key)
        if (not _identifier(config.account_id) or not isinstance(config.api_key, str)
                or not config.api_key or '\r' in config.api_key or '\n' in config.api_key):
            raise UnipileConfigurationError('Valid Unipile connection settings are required.')
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self._verified_owner = None
        self._verified_chat_ids = set()

    @classmethod
    def from_environment(cls, **kwargs):
        config = UnipileConfig.from_environment()
        return None if config is None else cls(config, **kwargs)

    def _read(self, path, params=None):
        try:
            response = self.session.get(
                self.config.base_url + path, params=params,
                headers={'X-API-KEY': self.config.api_key, 'accept': 'application/json'},
                timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:
            raise UnipileReadError('Unipile read could not be verified.') from None
        if response.status_code != 200:
            raise UnipileReadError('Unipile read was not successful.', response.status_code)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise UnipileReadError('Unipile read returned an invalid response.', response.status_code) from None
        if not isinstance(payload, dict):
            raise UnipileReadError('Unipile read returned an invalid response.', response.status_code)
        return payload

    def verify_account(self, expected_public_identifier=None):
        """Read the connected account, optionally matching the intended owner.

        Caller receives only identifying profile fields, never the email or
        private LinkedIn subscription and organization metadata returned by API.
        A failed verification clears any earlier authorization marker.
        """
        self._verified_owner = None
        self._verified_chat_ids.clear()
        payload = self._read('/users/me', {'account_id': self.config.account_id})
        if (payload.get('object') != 'AccountOwnerProfile' or payload.get('provider') != 'LINKEDIN'
                or not _identifier(payload.get('provider_id'))):
            raise UnipileReadError('The connected Unipile account is not a verified LinkedIn profile.')
        identifier = payload.get('public_identifier')
        if (expected_public_identifier is not None
                and (not _identifier(expected_public_identifier)
                     or not isinstance(identifier, str)
                     or identifier.casefold() != expected_public_identifier.casefold())):
            raise UnipileReadError('The connected LinkedIn profile does not match the configured owner.')
        safe = {name: payload[name] for name in ('provider', 'provider_id', 'public_identifier',
                'public_profile_url', 'first_name', 'last_name', 'headline')
                if isinstance(payload.get(name), str)}
        self._verified_owner = safe
        return dict(safe)

    def _require_verified(self):
        if self._verified_owner is None:
            raise UnipileConfigurationError('Verify the connected LinkedIn owner before using this capability.')

    @staticmethod
    def _page_parameters(limit, cursor):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 250:
            raise ValueError('The page size must be between 1 and 250.')
        params = {'limit': limit}
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 8192 or any(
                    ord(char) < 32 for char in cursor):
                raise ValueError('A valid pagination cursor is required.')
            params['cursor'] = cursor
        return params

    @staticmethod
    def _page(payload, expected_object):
        if (payload.get('object') != expected_object or not isinstance(payload.get('items'), list)
                or not all(isinstance(row, dict) for row in payload['items'])
                or 'cursor' not in payload
                or (payload['cursor'] is not None and not isinstance(payload['cursor'], str))):
            raise UnipileReadError('Unipile pagination returned an invalid response.')
        return {'object': expected_object, 'items': payload['items'], 'cursor': payload['cursor']}

    def list_chats(self, *, limit=100, cursor=None, unread=None):
        """Return one page. Follow cursor explicitly; private rows stay private."""
        self._require_verified()
        params = self._page_parameters(limit, cursor)
        params.update(account_id=self.config.account_id, account_type='LINKEDIN')
        if unread is not None:
            if not isinstance(unread, bool):
                raise ValueError('Unread must be a boolean.')
            params['unread'] = str(unread).lower()
        page = self._page(self._read('/chats', params), 'ChatList')
        # Reject any cross-account rows even if the server ignored its filter.
        if any(row.get('account_id') != self.config.account_id or row.get('account_type') != 'LINKEDIN'
               or not _identifier(row.get('id')) for row in page['items']):
            raise UnipileReadError('Unipile returned a chat outside the verified LinkedIn account.')
        self._verified_chat_ids.update(row['id'] for row in page['items'])
        return page

    def _require_chat(self, chat_id):
        self._require_verified()
        if not _identifier(chat_id):
            raise ValueError('A valid existing chat ID is required.')
        if chat_id not in self._verified_chat_ids:
            raise UnipileConfigurationError('Read this chat from the verified account before using it.')

    def list_messages(self, chat_id, *, limit=100, cursor=None):
        self._require_chat(chat_id)
        params = self._page_parameters(limit, cursor)
        page = self._page(self._read(f'/chats/{chat_id}/messages', params), 'MessageList')
        if any(row.get('account_id') != self.config.account_id or row.get('chat_id') != chat_id
               for row in page['items']):
            raise UnipileReadError('Unipile returned messages outside the verified conversation.')
        return page

    def _write(self, method, path, fields, *, status, acknowledgement):
        self._require_verified()
        try:
            # None filenames force multipart text fields, matching v1 schema.
            response = self.session.request(
                method, self.config.base_url + path,
                files={name: (None, value) for name, value in fields.items()},
                headers={'X-API-KEY': self.config.api_key, 'accept': 'application/json'},
                timeout=self.timeout, allow_redirects=False)
        except requests.RequestException:
            raise UnipileWriteError('Unipile write has an uncertain outcome; reconcile before retrying.',
                                    uncertain=True) from None
        http_status = response.status_code
        if 400 <= http_status < 500 and http_status not in (408, 429):
            raise UnipileWriteError('Unipile rejected the write.', uncertain=False, http_status=http_status)
        if http_status != status:
            raise UnipileWriteError('Unipile write has an uncertain outcome; reconcile before retrying.',
                                    uncertain=True, http_status=http_status)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise UnipileWriteError('Unipile write acknowledgement could not be verified.',
                                    uncertain=True, http_status=http_status) from None
        if (not isinstance(payload, dict) or payload.get('object') != acknowledgement
                or (acknowledgement == 'MessageSent' and not _identifier(payload.get('message_id')))):
            raise UnipileWriteError('Unipile write acknowledgement could not be verified.',
                                    uncertain=True, http_status=http_status)
        return {name: payload[name] for name in ('object', 'message_id') if name in payload}

    def update_profile(self, headline, summary):
        """Apply the two prepared positioning fields once, with explicit ACK."""
        if (not isinstance(headline, str) or not 1 <= len(headline.strip()) <= 220
                or not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 2600):
            raise ValueError('A headline up to 220 characters and About text up to 2600 characters are required.')
        return self._write('PATCH', '/users/me/edit',
                           {'type': 'LINKEDIN', 'account_id': self.config.account_id,
                            'headline': headline.strip(), 'summary': summary.strip()},
                           status=200, acknowledgement='ProfileEdited')

    def reply_in_chat(self, chat_id, text):
        """Reply only in an existing conversation; caller owns durable dedup."""
        if not _identifier(chat_id) or not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000:
            raise ValueError('An existing chat ID and a message up to 8000 characters are required.')
        self._require_chat(chat_id)
        return self._write('POST', f'/chats/{chat_id}/messages',
                           {'account_id': self.config.account_id, 'text': text.strip()},
                           status=201, acknowledgement='MessageSent')
