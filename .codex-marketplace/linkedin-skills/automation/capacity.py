"""Read-only Publora capacity checks before scheduling a platform post.

The account-context snapshot is advisory. A later write can still be rejected
if another writer consumes capacity between this check and that write.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
import re

import requests

ACCOUNT_CONTEXT_URL = 'https://api.publora.com/api/v1/account-context'
UNVERIFIED = 'Publora publishing capacity could not be verified.'


def _date(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Timezone-aware schedule required')
    return value.astimezone(timezone.utc)


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _available(quota):
    if not isinstance(quota, dict) or 'limit' not in quota:
        return None
    if quota['limit'] is None:
        return True  # The documented unlimited-plan representation.
    limit, used, remaining = (quota.get(key) for key in ('limit', 'used', 'remaining'))
    if not all(_integer(value) for value in (limit, used, remaining)):
        return None
    if remaining > max(0, limit - used):
        return None
    return remaining > 0


def _evaluate(payload, platform_id, schedule):
    if not isinstance(payload, dict) or payload.get('success') is not True:
        return False, UNVERIFIED
    context = payload.get('context')
    if not isinstance(context, dict):
        return False, UNVERIFIED
    features = context.get('features')
    if not isinstance(features, dict):
        return False, UNVERIFIED
    if features.get('publishing') is not True or features.get('apiAccess') is not True:
        return False, 'Publora publishing or API access is unavailable for this account.'
    if 'allowedPlatforms' not in context:
        return False, UNVERIFIED
    platforms = context['allowedPlatforms']
    if platforms is not None and (not isinstance(platforms, list) or 'linkedin' not in platforms):
        return False, 'This Publora plan does not allow LinkedIn publishing.'
    try:
        if schedule <= _date(context.get('serverTime')):
            return False, 'The requested publishing time is no longer in the future.'
    except (TypeError, ValueError):
        return False, UNVERIFIED
    quotas = context.get('quotas')
    if not isinstance(quotas, dict):
        return False, UNVERIFIED
    monthly = quotas.get('monthlyPosts')
    if not isinstance(monthly, dict):
        return False, UNVERIFIED
    if monthly.get('scope') == 'connection':
        connections = monthly.get('connections')
        if not isinstance(connections, list):
            return False, UNVERIFIED
        matching = [row for row in connections if isinstance(row, dict)
                    and row.get('platformSelection') == platform_id]
        if len(matching) != 1:
            return False, UNVERIFIED
        monthly = matching[0]
    elif monthly.get('scope') != 'account':
        return False, UNVERIFIED
    monthly_available = _available(monthly)
    if monthly_available is None:
        return False, UNVERIFIED
    if not monthly_available:
        return False, 'Publora monthly publishing quota is full; wait for its reset.'
    queued = quotas.get('scheduledPosts')
    if not isinstance(queued, dict) or queued.get('scope') != 'account':
        return False, UNVERIFIED
    queued_available = _available(queued)
    if queued_available is None:
        return False, UNVERIFIED
    if not queued_available:
        return False, 'Publora scheduled-post queue is full; wait for a queued post to publish.'
    horizon = quotas.get('scheduleHorizon')
    if not isinstance(horizon, dict) or 'limit' not in horizon or 'maxScheduledDate' not in horizon:
        return False, UNVERIFIED
    if horizon['limit'] is None:
        if horizon['maxScheduledDate'] is not None:
            return False, UNVERIFIED
    else:
        if not _integer(horizon['limit']):
            return False, UNVERIFIED
        try:
            if schedule > _date(horizon['maxScheduledDate']):
                return False, 'Requested publishing time exceeds this Publora plan’s schedule horizon.'
        except (TypeError, ValueError):
            return False, UNVERIFIED
    return True, 'Publora has capacity for this scheduled LinkedIn post.'


def check_post_capacity(platform_id, scheduled_time):
    """Return (allowed, safe reason); never print response bodies or credentials."""
    if not isinstance(platform_id, str) or not re.fullmatch(r'linkedin-[A-Za-z0-9_-]+', platform_id):
        return False, 'A valid LinkedIn platform connection is required.'
    try:
        schedule = _date(scheduled_time)
    except (TypeError, ValueError):
        return False, 'A timezone-aware publishing time is required.'
    key = os.getenv('PUBLORA_API_KEY')
    if not key:
        return False, 'Publora API credentials are unavailable.'
    try:
        response = requests.get(ACCOUNT_CONTEXT_URL,
                                headers={'x-publora-key': key, 'User-Agent': 'Mozilla/5.0'},
                                params={'scheduledTime': schedule.isoformat()},
                                timeout=20, allow_redirects=False)
        if response.status_code != 200:
            return False, UNVERIFIED
        return _evaluate(response.json(), platform_id, schedule)
    except (requests.RequestException, TypeError, ValueError):
        return False, UNVERIFIED
