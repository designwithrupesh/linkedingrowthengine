"""Use Publora analytics to reduce repeated paid comment reads.

The documented COMMENT statistic is a count, not a comment feed. Publora
caches metrics for two hours, and LinkedIn analytics can lag new activity.
An unchanged count cannot establish that every comment has been seen. This
gate therefore requires a fresh comment read at least once every 24 hours
when analytics is available. Without verified analytics, it permits a
fallback read every six hours. It does not provide instant notifications.

Statistics use a read-only POST endpoint; this module never writes to LinkedIn.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

import requests

from automation.capacity import ACCOUNT_CONTEXT_URL

STATISTICS_URL = 'https://api.publora.com/api/v1/linkedin-post-statistics'
FALLBACK_SECONDS = 6 * 60 * 60
RECONCILE_SECONDS = 24 * 60 * 60
RETRY_SECONDS = 15 * 60
STATISTICS_INTERVAL_SECONDS = 2 * 60 * 60
POST_URN = re.compile(r'urn:li:(?:activity|share|ugcPost):\d{18,25}')


def _clock(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Comment signals require a timezone-aware clock')
    return now.astimezone(timezone.utc)


def _stamp(value):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        if parsed is not None and parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed.timestamp()
    except ValueError:
        pass
    return None


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class CommentSignals:
    """Decide whether a paid reader is due; retain only counts and timestamps.

    ``due`` reads ``reply_monitor.posts[urn].last_attempt_at`` and
    ``last_success_at`` written by the runner's existing ``record_poll``.
    The caller must record the actual comment-read outcome there and save
    state. A failed read does not acknowledge a changed count.
    """

    def __init__(self, platform_id, token, session=None):
        if not isinstance(platform_id, str) or not re.fullmatch(r'linkedin-[A-Za-z0-9_-]+', platform_id):
            raise ValueError('Comment signals require a valid LinkedIn connection')
        self.platform_id = platform_id
        self._token = token if isinstance(token, str) and token.strip() else None
        self._session = session if session is not None else requests
        self._context_checked = False
        self._analytics = None

    def _headers(self):
        return {'x-publora-key': self._token, 'User-Agent': 'Mozilla/5.0'}

    def has_analytics(self):
        """Return True/False only from verified features; otherwise None.

        Fetch account context once per instance. HTTP errors, redirects and
        malformed responses do not grant analytics or leak provider bodies.
        """
        if self._context_checked:
            return self._analytics
        self._context_checked = True
        if self._token is None:
            return None
        try:
            response = self._session.get(ACCOUNT_CONTEXT_URL, headers=self._headers(),
                                         timeout=20, allow_redirects=False)
            if response.status_code != 200:
                return None
            payload = response.json()
            if not isinstance(payload, dict):
                return None
            context = payload.get('context')
            features = context.get('features') if isinstance(context, dict) else None
            if (payload.get('success') is not True or not isinstance(features, dict)
                    or features.get('apiAccess') is not True):
                return None
            analytics = features.get('analytics')
            self._analytics = analytics if isinstance(analytics, bool) else None
        except (requests.RequestException, TypeError, ValueError):
            return None
        return self._analytics

    def _count(self, urn):
        try:
            response = self._session.post(STATISTICS_URL, headers=self._headers(),
                json={'postedId': urn, 'platformId': self.platform_id, 'queryType': 'COMMENT'},
                timeout=20, allow_redirects=False)
            if response.status_code != 200:
                return None, None
            payload = response.json()
            if (not isinstance(payload, dict) or payload.get('success') is not True
                    or not _integer(payload.get('count'))):
                return None, None
            cached = payload.get('cached')
            return payload['count'], cached if isinstance(cached, bool) else None
        except (requests.RequestException, TypeError, ValueError):
            return None, None

    @staticmethod
    def _cached_due(signal, monitor, now):
        attempt = _stamp(monitor.get('last_attempt_at'))
        success = _stamp(monitor.get('last_success_at'))
        clock = now.timestamp()
        if signal.get('status') != 'analytics-count' or not _integer(signal.get('count')):
            return attempt is None or clock - attempt >= FALLBACK_SECONDS
        changed = _stamp(signal.get('changed_at'))
        pending_change = changed is not None and (success is None or success < changed)
        reconciliation = success is None or clock - success >= RECONCILE_SECONDS
        if not pending_change and not reconciliation:
            return False
        failed_attempt = attempt is not None and (success is None or attempt > success)
        return not (failed_attempt and clock - attempt < RETRY_SECONDS)

    def select(self, posts, state, now, *, limit=5):
        """Choose at most five fair signal candidates before any API requests.

        The durable consideration clock rotates pending reads and unavailable
        statistics across restarts. Pagination stays eligible without requiring
        another statistics request; unchanged cached counts wait two hours.
        """
        now = _clock(now)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError('Signal selection requires a nonnegative limit')
        signals = state.setdefault('comment_signals', {}).setdefault('posts', {})
        monitors = state.get('reply_monitor', {}).get('posts', {})
        ranked = []
        for rank, post in enumerate(posts):
            urn = post.get('post_urn') if isinstance(post, dict) else None
            if not isinstance(urn, str) or not POST_URN.fullmatch(urn):
                raise ValueError('Comment signals require a verified post URN')
            signal, monitor = signals.get(urn, {}), monitors.get(urn, {})
            checked = _stamp(signal.get('last_checked_at'))
            check_due = checked is None or now.timestamp() - checked >= STATISTICS_INTERVAL_SECONDS
            if monitor.get('next_page') or check_due or self._cached_due(signal, monitor, now):
                ranked.append((_stamp(signal.get('last_considered_at')) or 0, rank, post))
        chosen = [item[2] for item in sorted(ranked, key=lambda item: item[:2])[:min(limit, 5)]]
        for post in chosen:
            signals.setdefault(post['post_urn'], {})['last_considered_at'] = now.isoformat()
        return chosen

    def due(self, post, state, now):
        """Return whether to fetch comments, using the exact verified post URN.

        Unknown analytics/counts use bounded six-hour fallback polling. Valid
        count changes stay pending until a successful comment read. Failed
        attempts back off for 15 minutes while keeping that pending signal.
        """
        now = _clock(now)
        urn = post.get('post_urn') if isinstance(post, dict) else None
        if not isinstance(urn, str) or not POST_URN.fullmatch(urn):
            raise ValueError('Comment signals require a verified post URN')
        signals = state.setdefault('comment_signals', {}).setdefault('posts', {})
        signal = signals.setdefault(urn, {})
        monitor = state.get('reply_monitor', {}).get('posts', {}).get(urn, {})
        attempt = _stamp(monitor.get('last_attempt_at'))
        success = _stamp(monitor.get('last_success_at'))
        clock = now.timestamp()
        checked = _stamp(signal.get('last_checked_at'))
        if checked is not None and clock - checked < STATISTICS_INTERVAL_SECONDS:
            return self._cached_due(signal, monitor, now)

        def fallback(status):
            signal.update(status=status, last_checked_at=now.isoformat())
            return attempt is None or clock - attempt >= FALLBACK_SECONDS

        analytics = self.has_analytics()
        if analytics is not True:
            return fallback('fallback-no-analytics' if analytics is False else 'fallback-unverified-analytics')
        count, cached = self._count(urn)
        if count is None:
            return fallback('fallback-unverified-count')
        if signal.get('count') != count or not _integer(signal.get('count')):
            signal['changed_at'] = now.isoformat()
        signal.update(count=count, cached=cached, status='analytics-count', last_checked_at=now.isoformat())
        return self._cached_due(signal, monitor, now)
