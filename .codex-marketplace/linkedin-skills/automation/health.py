"""Read-only local receipt health; no API calls or state changes.

A scheduled receipt establishes provider acknowledgement, not publication.
Missing receipts, uncertain writes and old successful validation are never
presented as confirmed delivery. Exit status is normally zero; --require-buffer
also requires the next local publication day to have acknowledged slots.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
_SLOT = re.compile(r'^post:(\d{4}-\d{2}-\d{2})(?::([0-2]\d)00)?$')
_SAFE = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
_ACKNOWLEDGED = {'scheduled', 'published'}
HEARTBEAT_MAX_SECONDS = 900


def _stamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else None
        return result if result is not None and result.tzinfo is not None and result.utcoffset() is not None else None
    except ValueError:
        return None


def _safe(value, default='unavailable'):
    return value if isinstance(value, str) and _SAFE.fullmatch(value) else default


def _post_slots(state, zone):
    slots = {}
    for key, action in state.get('actions', {}).items():
        if not isinstance(action, dict) or action.get('kind') != 'post':
            continue
        match = _SLOT.fullmatch(key) if isinstance(key, str) else None
        schedule = _stamp(action.get('scheduled_for'))
        if match:
            day, hour = match[1], int(match[2] or 9)
        elif schedule is not None:
            local = schedule.astimezone(zone)
            day, hour = local.date().isoformat(), local.hour
        else:
            continue
        if action.get('allocation_day', day) != day:
            continue
        slots.setdefault((day, hour), []).append(action)
    return slots


def _slot_state(actions, day, zone):
    if not actions:
        return 'missing'
    if len(actions) > 1:
        return 'multiple-intents'
    action = actions[0]
    schedule = _stamp(action.get('scheduled_for'))
    if (action.get('status') in _ACKNOWLEDGED and action.get('remote_id')
            and schedule is not None and schedule.astimezone(zone).date().isoformat() == day):
        return 'acknowledged'
    return 'rejected' if action.get('status') == 'rejected' else 'unverified'


def build_health(state, policy, now=None):
    """Classify current local evidence without inferring unobserved outcomes."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Health requires a timezone-aware clock')
    zone = ZoneInfo(policy.get('timezone', 'Asia/Calcutta'))
    local, today = now.astimezone(zone), now.astimezone(zone).date()
    hours = policy.get('posting_hours', [policy['posting_hour']] if 'posting_hour' in policy else [9, 18])
    if (not isinstance(hours, (list, tuple)) or not hours
            or any(type(hour) is not int or not 0 <= hour <= 23 for hour in hours)
            or len(set(hours)) != len(hours)):
        raise ValueError('Health requires valid posting hours')
    limit = policy.get('max_posts_per_day', 2)
    if type(limit) is not int or limit < 0:
        raise ValueError('Health requires a valid daily post limit')
    hours = sorted(hours)[:limit]
    weekdays = policy.get('posting_days', list(range(7)))
    if not isinstance(weekdays, (list, tuple)) or any(type(day) is not int or not 0 <= day <= 6 for day in weekdays):
        raise ValueError('Health requires valid posting days')
    slots = _post_slots(state, zone)

    def day_report(day):
        date = day.isoformat()
        report = {'date': date, 'required': 0, 'acknowledged': [], 'missing': [],
                  'unverified': [], 'rejected': [], 'multiple-intents': []}
        for hour in hours if day.weekday() in weekdays else []:
            key = f'post:{date}:{hour:02d}00'
            category = _slot_state(slots.get((date, hour), []), date, zone)
            report[category].append(key)
            report['required'] += 1
        report['ready'] = len(report['acknowledged']) == report['required']
        return report

    current = day_report(today)
    delay = policy.get('posting_max_delay_minutes', 120)
    if type(delay) is not int or not 0 <= delay <= 120:
        raise ValueError('Health requires a valid posting delay')
    current['missed_without_intent'] = [key for key in current['missing']
        if local > local.replace(hour=int(key[-4:-2]), minute=0, second=0, microsecond=0)
        + timedelta(minutes=delay)]
    # Unknown or rejected intents are separate: absence of a successful local
    # receipt does not prove an ambiguous remote operation never published.
    buffer = [day_report(today + timedelta(days=offset)) for offset in range(1, 4)]
    next_publication = next((day for day in buffer if day['required']), None)
    if next_publication is None:
        next_publication = next((day_report(today + timedelta(days=offset)) for offset in range(4, 8)
                                 if (today + timedelta(days=offset)).weekday() in weekdays and hours), None)
    next_day_ready = next_publication is None or next_publication['ready']
    buffer_ready = all(day['ready'] for day in buffer)

    interactions = {name: {kind: 0 for kind in ('reaction', 'comment', 'reply')}
                    for name in ('attempts', 'confirmed_sent', 'uncertain', 'rejected')}
    for action in state.get('actions', {}).values():
        if not isinstance(action, dict) or action.get('kind') not in interactions['attempts']:
            continue
        stamp = _stamp(action.get('at'))
        if stamp is None or stamp.astimezone(zone).date() != today:
            continue
        kind, status = action['kind'], action.get('status')
        interactions['attempts'][kind] += 1
        if status == 'sent' and action.get('remote_id'):
            interactions['confirmed_sent'][kind] += 1
        elif status == 'rejected':
            interactions['rejected'][kind] += 1
        elif status in ('sent', 'inflight', 'unknown-needs-reconciliation'):
            interactions['uncertain'][kind] += 1
    allocated = state.get('days', {}).get(today.isoformat(), {})
    interactions['allocated_counters'] = {key: value for key, value in allocated.items()
        if key in ('post', 'interaction', 'public_interaction', 'reply', 'read')
        and type(value) is int and value >= 0}

    operational = state.get('operational_status', {})
    saved_session = operational.get('session', {})
    heartbeat = _stamp(saved_session.get('heartbeat_at'))
    age = int((now - heartbeat).total_seconds()) if heartbeat is not None else None
    session_status = _safe(saved_session.get('status'), 'not-observed')
    if session_status == 'running' and (age is None or age < 0 or age > HEARTBEAT_MAX_SECONDS):
        session_status = 'stale'
    errors = []
    for error in saved_session.get('recent_errors', [])[-6:]:
        if not isinstance(error, dict):
            continue
        at = _stamp(error.get('at'))
        if at is not None and timedelta(0) <= now - at <= timedelta(days=1):
            errors.append({'phase': _safe(error.get('phase')), 'type': _safe(error.get('type')),
                           'at': at.isoformat()})
    session = {'status': session_status, 'heartbeat_age_seconds': age,
               'heartbeat_at': heartbeat.isoformat() if heartbeat is not None else None,
               'recent_errors': errors}
    start, end = policy.get('public_engagement_start_hour', 17), policy.get('public_engagement_end_hour', 22)
    active_window = start <= local.hour < end
    session_expected = active_window and (local.hour * 60 + local.minute - start * 60) >= 15

    queue = operational.get('queue_preparation', {})
    queue_at = _stamp(queue.get('checked_at'))
    queue_failed = (queue.get('status') == 'deferred' and queue_at is not None
                    and timedelta(0) <= now - queue_at <= timedelta(days=1))
    drafting = {'ai_status': _safe(operational.get('ai'), 'not-observed'),
                'queue_status': _safe(queue.get('status'), 'not-observed'),
                'latest_error_type': _safe(queue.get('error_type')) if queue_failed else None,
                'recent_errors': [error for error in errors if error['phase'] in ('post', 'next-day-posts')],
                'unclaimed_attempts': []}
    for key, attempt in state.get('post_queue', {}).get('attempts', {}).items():
        if not isinstance(key, str) or not _SLOT.fullmatch(key) or not isinstance(attempt, dict):
            continue
        match = _SLOT.fullmatch(key)
        slot_status = _slot_state(slots.get((match[1], int(match[2] or 9)), []), match[1], zone)
        at, retry = _stamp(attempt.get('attempted_at')), _stamp(attempt.get('retry_after'))
        if slot_status != 'missing' or match[1] < today.isoformat() or at is None:
            continue
        drafting['unclaimed_attempts'].append({'slot': key, 'attempted_at': at.isoformat(),
            'retry_after': retry.isoformat() if retry is not None else None,
            'status': 'waiting-for-retry' if retry is not None and now < retry else 'retry-due'})
    drafting['unclaimed_attempts'].sort(key=lambda item: item['attempted_at'], reverse=True)
    drafting['unclaimed_attempts'] = drafting['unclaimed_attempts'][:6]

    cooldown = state.get('comment_signals', {}).get('statistics_cooldown', {})
    until = _stamp(cooldown.get('until'))
    cooling = until is not None and now < until
    analytics = {'status': 'cooldown' if cooling else 'not-currently-held',
                 'resource': 'memberCreatorPostAnalytics:FINDER:entity',
                 'until': until.isoformat() if cooling else None,
                 'scope': cooldown.get('scope') if cooldown.get('scope') in ('member', 'application') else 'unknown',
                 'affects': ['statistics'], 'posting_or_public_writes_blocked': False}
    reasons, limitations = [], []
    if not buffer_ready or not next_day_ready:
        reasons.append('posting-buffer-incomplete')
    if current['missed_without_intent']:
        reasons.append('due-post-slot-missing-intent')
    if current['unverified'] or current['rejected'] or current['multiple-intents']:
        reasons.append('today-post-intent-needs-reconciliation')
    if session_status == 'stale' or (session_expected and session_status != 'running'):
        reasons.append('engagement-session-not-fresh')
    if any(error['phase'] != 'analytics' for error in errors) or queue_failed:
        reasons.append('recent-operation-errors')
    if cooling:
        limitations.append('statistics-cooldown')
    if drafting['ai_status'] == 'budget-paused':
        limitations.append('ai-budget-paused')
    return {'observed_at': now.isoformat(), 'timezone': str(zone), 'evidence': 'local-receipts-only',
            'status': 'degraded' if reasons else 'limited' if limitations else 'ready',
            'reasons': reasons, 'limitations': limitations,
            'posting': {'today': current, 'buffer': buffer, 'buffer_ready': buffer_ready,
                        'next_publication_day': next_publication['date'] if next_publication else None,
                        'next_day_ready': next_day_ready},
            'interactions': interactions, 'session': session, 'drafting': drafting,
            'analytics': analytics}


def summary(report):
    """Render only fixed diagnostics and public slot identities."""
    posting, interactions = report['posting'], report['interactions']
    lines = ['## Autopilot health', '', f"Status: **{report['status']}** ({report['observed_at']}).",
             'Evidence: local provider acknowledgements; publication is not reverified here.', '',
             '| Local publication day | Required slots | Acknowledged slots | Missing or unresolved |',
             '| --- | ---: | ---: | ---: |']
    for day in posting['buffer']:
        count = len(day['acknowledged'])
        lines.append(f"| {day['date']} | {day['required']} | {count} | {day['required'] - count} |")
    lines.extend(['', 'Today\'s confirmed sent interactions: ' + json.dumps(interactions['confirmed_sent']) + '.',
                  'Today\'s recorded attempts: ' + json.dumps(interactions['attempts']) + '.',
                  'Session: ' + report['session']['status'] + '.',
                  'Statistics: ' + report['analytics']['status'] + '.',
                  'Reasons: ' + (', '.join(report['reasons']) or 'none') + '.',
                  'Limitations: ' + (', '.join(report['limitations']) or 'none') + '.'])
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=ROOT / 'automation/state.json')
    parser.add_argument('--policy', type=Path, default=ROOT / 'automation/policy.json')
    parser.add_argument('--summary', action='store_true', help='print Markdown instead of JSON')
    parser.add_argument('--require-buffer', action='store_true', help='exit 2 if the next publication day lacks acknowledged slots')
    args = parser.parse_args(argv)
    state = json.loads(args.state.read_text(encoding='utf-8'))
    policy = json.loads(args.policy.read_text(encoding='utf-8'))
    report = build_health(state, policy)
    print(summary(report) if args.summary else json.dumps(report, indent=2))
    return 2 if args.require_buffer and not report['posting']['next_day_ready'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
