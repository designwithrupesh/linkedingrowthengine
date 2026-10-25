"""Bounded, public LinkedIn post discovery for the autonomous runner.

Discovery uses one profile actor call at a time. The caller supplies its
``read`` checkpoint callback so this request shares the runner's daily quota.
No profile handles are built in; policy explicitly selects the source profiles.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import math
import os
import re
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

import requests

from lib.apify_client import ApifyClient, ApifyError


PROFILE_POSTS_ACTOR = 'apimaestro~linkedin-profile-posts'
_URN = re.compile(r'urn:li:(?:activity|share|ugcPost):[0-9]{18,25}')
_HANDLE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}')
_POST_SLUG = re.compile(r'(?:^|[-_])(?P<kind>activity|share|ugcPost)-(?P<id>[0-9]{18,25})(?:-|$)')
_HOSTS = {'linkedin.com', 'www.linkedin.com'}


def profile_handle(value: Any) -> str | None:
    """Accept a handle or a real public LinkedIn profile URL, never a hostname lookalike."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if _HANDLE.fullmatch(value):
        return value.casefold()
    parsed = _linkedin_url(value)
    if parsed is None:
        return None
    match = re.fullmatch(r'/in/([^/]+)/?', unquote(parsed.path))
    if match and _HANDLE.fullmatch(match[1]):
        return match[1].casefold()
    return None


def _linkedin_url(value: str):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in _HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            return None
        return parsed
    except ValueError:
        return None


def post_urn(value: Any) -> str | None:
    """Parse only complete post URNs and validated public post paths.

    Query text and comment URNs cannot smuggle an unrelated post identity into
    the parser. Canonicalizing the URL also strips tracking parameters.
    """
    if not isinstance(value, str):
        return None
    value = value.strip()
    if _URN.fullmatch(value):
        return value
    parsed = _linkedin_url(value)
    if parsed is None:
        return None
    path = unquote(parsed.path)
    match = re.fullmatch(r'/feed/update/(urn:li:(?:activity|share|ugcPost):[0-9]{18,25})/?', path)
    if match:
        return match[1]
    match = re.fullmatch(r'/posts/([^/]+)/?', path)
    slug = _POST_SLUG.search(match[1]) if match else None
    if slug:
        return f'urn:li:{slug["kind"]}:{slug["id"]}'
    return None


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _first_text(*values: Any) -> str | None:
    return next((value.strip() for value in values if isinstance(value, str) and value.strip()), None)


def _typed_urn(value: Any, kind: str) -> str | None:
    """Only typed actor fields may turn numeric identifiers into their own URN type."""
    candidate = post_urn(value)
    if candidate:
        return candidate if candidate.startswith(f'urn:li:{kind}:') else None
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return post_urn(f'urn:li:{kind}:{value}')
    return None


def _posted_at(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, dict):
            stamp = _posted_at(value.get('date'), value.get('timestamp'), value.get('iso'))
            if stamp:
                return stamp
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                # Undated relative strings ("2d") do not provide a useful clock.
                if parsed.tzinfo is not None:
                    return parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                continue
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            try:
                return datetime.fromtimestamp(value / 1000 if value > 100_000_000_000 else value,
                                              timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                continue
    return None


def normalize_profile_post(raw: dict, source_handle: str) -> dict | None:
    """Normalize flat and nested actor post records without descending into reshares."""
    if not isinstance(raw, dict) or 'summary' in raw:
        return None
    post = _mapping(raw.get('post')) or raw
    author = _mapping(raw.get('author')) or _mapping(post.get('author'))
    urn_fields = _mapping(post.get('urn'))
    urn = next((parsed for value in (
        post.get('full_urn'), post.get('urn'), post.get('post_urn'), post.get('postUrn'), post.get('shareUrn'),
        raw.get('full_urn'), raw.get('urn'), raw.get('post_urn'),
    ) if (parsed := post_urn(value))), None)
    if not urn:
        for field, kind in (('activity_urn', 'activity'), ('share_urn', 'share'), ('ugcPost_urn', 'ugcPost')):
            value = urn_fields.get(field) or post.get(field)
            candidate = _typed_urn(value, kind)
            if candidate:
                urn = candidate
                break
    url_urn = next((parsed for value in (
        post.get('url'), post.get('post_url'), post.get('postUrl'), post.get('linkedinUrl'),
        raw.get('url'), raw.get('post_url'), raw.get('postUrl'), raw.get('linkedinUrl'),
    ) if (parsed := post_urn(value))), None)
    share_urn = next((parsed for value in (post.get('full_urn'), post.get('shareUrn'), raw.get('full_urn'),
                                         raw.get('shareUrn'), post.get('urn'))
                      if (parsed := post_urn(value)) and parsed.startswith(('urn:li:share:', 'urn:li:ugcPost:'))), None)
    if share_urn is None:
        share_urn = next((parsed for field, kind in (('share_urn', 'share'), ('share_id', 'share'),
                                                    ('ugcPost_urn', 'ugcPost'), ('ugcPost_id', 'ugcPost'))
                          for value in (urn_fields.get(field), post.get(field), raw.get(field))
                          if (parsed := _typed_urn(value, kind))), None)
    # Public activity links and writable share/UGC identifiers can have
    # different digits. Preserve both, using only explicitly supplied typed
    # share metadata for the canonical writable identifier.
    urn = share_urn or urn or url_urn
    text = _first_text(post.get('text'), post.get('content'), post.get('post_text'), raw.get('text'))
    if not urn or not text:
        return None
    author_url = _first_text(author.get('profile_url'), author.get('profileUrl'), author.get('url'),
                             post.get('authorProfileUrl'), raw.get('authorProfileUrl'))
    author_handle = profile_handle(author_url) or profile_handle(author.get('username')) or source_handle
    author_name = ' '.join(value for value in (author.get('first_name'), author.get('last_name'))
                           if isinstance(value, str) and value.strip())
    return {
        'url': f'https://www.linkedin.com/feed/update/{url_urn or urn}/',
        'urn': urn,
        'shareUrn': share_urn,
        'text': text,
        'authorName': _first_text(author.get('name'), author_name, post.get('authorName'), raw.get('authorName')),
        'authorHeadline': _first_text(author.get('headline')),
        'authorProfileUrl': f'https://www.linkedin.com/in/{author_handle}/',
        'postedAtISO': _posted_at(post.get('postedAtISO'), post.get('created_at'), post.get('posted_at'),
                                  post.get('postedAt'), raw.get('posted_at')),
        'origin': 'profile-discovery',
        'sourceProfile': f'https://www.linkedin.com/in/{source_handle}/',
    }


def _post_rows(payload: Any, depth: int = 0):
    # Only known collection wrappers are traversed. Author records, replies and
    # reshared-post metadata are not separate engagement targets.
    if depth > 4:
        return
    if isinstance(payload, list):
        for row in payload[:100]:
            yield from _post_rows(row, depth + 1)
    elif isinstance(payload, dict):
        for field in ('data', 'posts', 'items', 'results'):
            wrapper = payload.get(field)
            if isinstance(wrapper, (dict, list)):
                yield from _post_rows(wrapper, depth + 1)
                return
        yield payload


class DiscoveryClient(ApifyClient):
    """Existing read client plus a bounded profile-post discovery operation."""

    def fetch_profile_posts(self, username: str, *, limit: int = 5) -> list[dict]:
        handle = profile_handle(username)
        if not handle:
            raise ValueError('A valid public LinkedIn profile handle is required')
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5:
            raise ValueError('Profile discovery limit must be between 1 and 5')
        # These are the actor's verified input fields. No pagination loop.
        payload = self._run_sync(PROFILE_POSTS_ACTOR,
                                 {'username': handle, 'limit': limit, 'page_number': 1})
        result, seen = [], set()
        for row in _post_rows(payload):
            post = normalize_profile_post(row, handle)
            if post and post['urn'] not in seen:
                result.append(post)
                seen.add(post['urn'])
            if len(result) == limit:
                break
        return result


def _discovery_source(policy: dict, state: dict, now: datetime | None):
    """Select the next expired source without changing its durable cursor."""
    own = profile_handle(policy.get('profile_url'))
    sources = list(dict.fromkeys(handle for value in policy.get('discovery_profiles') or []
                                if (handle := profile_handle(value)) and handle != own))
    if not sources:
        return None
    cursor = state.get('discovery', {}).get('profile_cursor', 0)
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        cursor = 0
    if now is None:
        return sources[cursor % len(sources)], cursor + 1
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('A timezone-aware discovery clock is required')
    ttl = policy.get('target_cache_ttl_hours', 6)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 24:
        raise ValueError('Discovery cache TTL must be between 1 and 24 hours')
    checked = state.get('engagement', {}).get('profile_checked_at', {})
    for offset in range(len(sources)):
        handle = sources[(cursor + offset) % len(sources)]
        try:
            stamp = datetime.fromisoformat(checked[handle].replace('Z', '+00:00'))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                stamp = None
        except (KeyError, TypeError, AttributeError, ValueError):
            stamp = None
        if stamp is None or now.astimezone(timezone.utc) - stamp >= timedelta(hours=ttl):
            return handle, cursor + offset + 1
    return None


def discover_targets(policy: dict, reader: DiscoveryClient, state: dict,
                     read: Callable | None = None, *, now: datetime | None = None,
                     checkpoint: Callable | None = None) -> list[dict]:
    """Read one configured profile and rotate sources across durable runs.

    Pass ``read=runner.read`` so the quota and account guard run first. With
    ``now`` and ``checkpoint=runner.save``, reserve the selected source only
    after those guards allow a read, but before its actor can start. An
    uncertain actor attempt therefore keeps its TTL while a denied read does
    not consume freshness or strand the rotation on one profile.
    """
    own = profile_handle(policy.get('profile_url'))
    source = _discovery_source(policy, state, now)
    if source is None:
        return []
    handle, next_cursor = source
    discovery = state.setdefault('discovery', {})

    def fetch():
        discovery['profile_cursor'] = next_cursor
        if now is not None:
            state.setdefault('engagement', {}).setdefault('profile_checked_at', {})[handle] = (
                now.astimezone(timezone.utc).isoformat())
        if checkpoint is not None:
            checkpoint()
        return reader.fetch_profile_posts(handle, limit=5)

    try:
        rows = read(fetch) if read is not None else fetch()
    except (ApifyError, requests.RequestException):
        discovery['last_status'] = 'source-unavailable'
        return []
    if rows is None:
        discovery['last_status'] = 'daily-budget-exhausted'
        return []
    discovery['last_status'] = 'ok'
    return [post for post in rows if isinstance(post, dict)
            and post_urn(post.get('url')) and post_urn(post.get('urn'))
            and _first_text(post.get('text'))
            and profile_handle(post.get('authorProfileUrl')) != own]


def read_budget_available(reader: ApifyClient) -> bool:
    """Fail closed if free-credit usage cannot be verified; posting can continue.

    Four USD is a ceiling, not a promise of free service. The account's lower
    configured limit wins, reserving at least one USD of a five-USD allowance.
    No actor is run by this check and no provider body or credential is logged.
    """
    try:
        budget = float(os.getenv('APIFY_MONTHLY_BUDGET_USD') or '4')
        if not math.isfinite(budget) or budget <= 0:
            return False
        response = reader._session.get(reader.BASE_URL + '/users/me/limits',
                                       headers={'Authorization': 'Bearer ' + reader.token},
                                       timeout=30, allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200:
            return False
        payload = response.json()
        data = (_mapping(payload.get('data')) or payload) if isinstance(payload, dict) else {}
        usage = _mapping(data.get('current')).get('monthlyUsageUsd')
        cap = _mapping(data.get('limits')).get('maxMonthlyUsageUsd')
        valid = lambda number: (isinstance(number, (int, float)) and not isinstance(number, bool)
                                and math.isfinite(number))
        return bool(valid(usage) and valid(cap) and 0 <= usage < min(cap, budget))
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return False
