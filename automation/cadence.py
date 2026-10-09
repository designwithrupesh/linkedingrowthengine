"""Deterministic daily post slots without catch-up bursts or duplicate seeds.

The runner wakes at the nominal slot, then schedules publication five minutes
ahead. A late session may claim that same slot for up to two hours. Older
``post:DATE`` receipts reserve the 09:00 slot and remain part of the daily quota.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from typing import Mapping
from zoneinfo import ZoneInfo


DEFAULT_POSTING_HOURS = (9, 18)
MAX_SESSION_DELAY_MINUTES = 120
PUBLICATION_LEAD_MINUTES = 5
_POST_KEY = re.compile(r"^post:(\d{4}-\d{2}-\d{2})(?::(?:[01]\d|2[0-3])[0-5]\d)?$")


@dataclass(frozen=True)
class PostSlot:
    key: str
    allocation_day: str
    nominal_time: datetime
    scheduled_time: datetime


def _day(value: date | str) -> str:
    if isinstance(value, datetime):
        raise ValueError('A publication date, not a datetime, is required')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return value
    raise ValueError('A canonical publication date is required')


def slot_key(day: date | str, hour: int, minute: int = 0) -> str:
    """Return the same identity even when a delayed session changes publish time."""
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError('Posting hour must be an integer from 0 through 23')
    if type(minute) is not int or not 0 <= minute <= 59:
        raise ValueError('Posting minute must be an integer from 0 through 59')
    return f'post:{_day(day)}:{hour:02d}{minute:02d}'


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Timezone-aware clock required')
    return value


def _action_day(key, action, zone):
    allocation = action.get('allocation_day')
    if allocation is not None:
        return _day(allocation)
    scheduled = action.get('scheduled_for')
    if scheduled:
        when = _aware(datetime.fromisoformat(scheduled.replace('Z', '+00:00')))
        return when.astimezone(zone).date().isoformat()
    match = _POST_KEY.fullmatch(key) if isinstance(key, str) else None
    if match:
        return _day(match.group(1))
    recorded = action.get('at')
    if recorded:
        when = _aware(datetime.fromisoformat(recorded.replace('Z', '+00:00')))
        return when.astimezone(zone).date().isoformat()
    return None


def claimed_post_count(day: date | str, state: Mapping, timezone_name: str = 'Asia/Calcutta') -> int:
    """Honor both durable daily counters and every post intent, including unknowns.

    Taking the larger count repairs a missing/stale counter without counting the
    same recorded intent twice. Rejected and uncertain writes stay reserved.
    """
    allocation_day = _day(day)
    zone = ZoneInfo(timezone_name)
    actions = state.get('actions', {})
    days = state.get('days', {})
    if not isinstance(actions, Mapping) or not isinstance(days, Mapping):
        raise ValueError('Post state must contain action and daily mappings')
    counter = days.get(allocation_day, {})
    if not isinstance(counter, Mapping):
        raise ValueError('Daily post counter must be a mapping')
    recorded_count = counter.get('post', 0)
    if type(recorded_count) is not int or recorded_count < 0:
        raise ValueError('Daily post counter must be a nonnegative integer')
    count = 0
    for key, action in actions.items():
        if not isinstance(action, Mapping):
            raise ValueError('Post action must be a mapping')
        is_post = action.get('kind') == 'post' or (isinstance(key, str) and _POST_KEY.fullmatch(key))
        if is_post and _action_day(key, action, zone) == allocation_day:
            count += 1
    return max(recorded_count, count)


def due_slots(now: datetime, policy: Mapping, state: Mapping) -> list[PostSlot]:
    """Return today's remaining slots only during their bounded session window.

    Defaults are 09:00 and 18:00 on all seven days. ``posting_hours`` overrides
    the old single ``posting_hour`` field. Late publication remains on the same
    local date; a missed slot is skipped rather than backfilled by a later run.
    """
    local = _aware(now).astimezone(ZoneInfo(policy.get('timezone', 'Asia/Calcutta')))
    day = local.date().isoformat()
    days = policy.get('posting_days', list(range(7)))
    if not isinstance(days, (list, tuple)) or any(type(value) is not int or not 0 <= value <= 6 for value in days):
        raise ValueError('Posting days must be weekday integers from 0 through 6')
    hours = policy.get('posting_hours')
    if hours is None:
        hours = [policy['posting_hour']] if 'posting_hour' in policy else DEFAULT_POSTING_HOURS
    if not isinstance(hours, (list, tuple)) or not hours:
        raise ValueError('Posting hours must be a nonempty sequence')
    if any(type(hour) is not int or not 0 <= hour <= 23 for hour in hours) or len(set(hours)) != len(hours):
        raise ValueError('Posting hours must be distinct integers from 0 through 23')
    delay = policy.get('posting_max_delay_minutes', MAX_SESSION_DELAY_MINUTES)
    if type(delay) is not int or not 0 <= delay <= MAX_SESSION_DELAY_MINUTES:
        raise ValueError('Posting delay must be an integer from 0 through 120 minutes')
    limit = policy.get('max_posts_per_day', 2)
    if type(limit) is not int or limit < 0:
        raise ValueError('Daily post limit must be a nonnegative integer')
    remaining = limit - claimed_post_count(day, state, policy.get('timezone', 'Asia/Calcutta'))
    if local.weekday() not in days or remaining <= 0:
        return []
    publication = local + timedelta(minutes=PUBLICATION_LEAD_MINUTES)
    if publication.date() != local.date():
        return []
    actions = state.get('actions', {})
    result = []
    for hour in sorted(hours):
        nominal = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if not nominal <= local <= nominal + timedelta(minutes=delay):
            continue
        key = slot_key(day, hour)
        if key in actions or (hour == 9 and 'post:' + day in actions):
            continue
        result.append(PostSlot(key, day, nominal.astimezone(timezone.utc), publication.astimezone(timezone.utc)))
        if len(result) >= remaining:
            break
    return result
