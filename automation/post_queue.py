"""Maintain a bounded rolling post buffer before publication slots arrive.

Publora owns the publication clock once a post is acknowledged. A delayed
GitHub wake-up therefore does not move tomorrow's morning post into midday.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from automation.cadence import DEFAULT_POSTING_HOURS, claimed_post_count, slot_key
from automation import apify_ai
from automation.visual_planning import select_brief_format, prepare_visual
from automation import design_news


ROOT = Path(__file__).resolve().parents[1]
DRAFT_COOLDOWN_SECONDS = 2 * 60 * 60
MAX_PREPARATION_ATTEMPTS_PER_RUN = 2


def _future_slots(runner):
    now, policy = runner.now, runner.policy
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('A timezone-aware post preparation clock is required')
    local = now.astimezone(ZoneInfo(policy.get('timezone', 'Asia/Calcutta')))
    days = policy.get('posting_days', list(range(7)))
    if not isinstance(days, (list, tuple)) or any(type(day) is not int or not 0 <= day <= 6 for day in days):
        raise ValueError('Posting days must be weekday integers from 0 through 6')
    hours = policy.get('posting_hours')
    if hours is None:
        hours = [policy['posting_hour']] if 'posting_hour' in policy else DEFAULT_POSTING_HOURS
    if (not isinstance(hours, (list, tuple)) or not hours
            or any(type(hour) is not int or not 0 <= hour <= 23 for hour in hours)
            or len(set(hours)) != len(hours)):
        raise ValueError('Posting hours must be distinct integers from 0 through 23')
    limit = policy.get('max_posts_per_day', 2)
    if type(limit) is not int or limit < 0:
        raise ValueError('Daily post limit must be a nonnegative integer')
    buffer_days = policy.get('posting_buffer_days', 1)
    if type(buffer_days) is not int or not 1 <= buffer_days <= 3:
        raise ValueError('Post buffer must be an integer from one through three days')
    if not limit:
        return []
    # Prepare any nominal slot still ahead today, then the next buffer dates.
    # Past slots never move forward into a catch-up publication.
    dates = [local.date() + timedelta(days=offset) for offset in range(buffer_days + 1)]
    result = []
    for day in dates:
        if day.weekday() not in days:
            continue
        for hour in sorted(hours):
            schedule = datetime.combine(day, datetime.min.time(), tzinfo=local.tzinfo).replace(hour=hour)
            if schedule > local:
                result.append((slot_key(day, hour), day.isoformat(), schedule.astimezone(timezone.utc)))
    return result


def _retry_due(attempt, now):
    if not attempt:
        return True
    if not isinstance(attempt, dict):
        raise ValueError('Post draft attempt must be a mapping')
    try:
        retry = datetime.fromisoformat(attempt['retry_after'].replace('Z', '+00:00'))
        if retry.tzinfo is None or retry.utcoffset() is None:
            raise ValueError
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ValueError('Post draft retry time must be timezone-aware') from None
    return now >= retry


def prepare_next_day_posts(runner):
    """Prepare today's future slots plus the next one to three local dates.

    Every post intent, including rejected and uncertain writes, remains claimed.
    Generation attempts have their own two-hour checkpoint; they do not consume
    a post quota or authorize a retry of a remote write. The two-slot invocation
    bound also applies to skipped generation, so an empty buffer cannot cause
    a burst of model requests. Existing callers keep this function's name.
    """
    slots = _future_slots(runner)
    if not slots:
        return []
    policy = runner.policy
    zone_name = policy.get('timezone', 'Asia/Calcutta')
    limit = min(2, policy.get('max_posts_per_day', 2))
    plan_path = ROOT / 'automation/content-plan.json'
    plan = json.loads(plan_path.read_text(encoding='utf-8')) if plan_path.exists() else {}
    from automation.twin import skills
    task = skills('linkedin-content-planner', 'linkedin-post-writer')
    if policy.get('source_notes'):
        task += '\n' + skills('linkedin-repurposer')
    attempted = []
    preparation_attempts = 0
    configured_hours = policy.get('posting_hours')
    if configured_hours is None:
        configured_hours = [policy['posting_hour']] if 'posting_hour' in policy else DEFAULT_POSTING_HOURS
    slot_hours = sorted(configured_hours)
    for key, day, schedule in slots:
        if preparation_attempts >= MAX_PREPARATION_ATTEMPTS_PER_RUN:
            break
        if claimed_post_count(day, runner.state, zone_name) >= limit:
            continue
        hour = schedule.astimezone(ZoneInfo(zone_name)).hour
        brief = next((item for item in plan.get('slots', [])
                      if item['date'] == day and item['hour'] == hour), None)
        static_visual = (select_brief_format(policy, day, hour, brief) != 'text'
                         and brief is not None
                         and ('media_ref' in brief or 'art_ref' in brief))
        if runner.model_paused and not static_visual:
            continue
        actions = runner.state['actions']
        if key in actions or (hour == 9 and 'post:' + day in actions):
            continue
        attempts = runner.state.get('post_queue', {}).get('attempts', {})
        if not isinstance(attempts, dict):
            raise ValueError('Post draft attempts must be a mapping')
        if not _retry_due(attempts.get(key), runner.now):
            continue
        if runner.live:
            allowed, reason = runner.post_capacity(policy['platform_id'], schedule)
            if not allowed:
                print('Next-day post deferred: ' + reason)
                continue
        # Reserve generation before a model request so crashes and malformed
        # responses cannot spend AI credit again at every polling cycle.
        attempts = runner.state.setdefault('post_queue', {}).setdefault('attempts', {})
        attempts[key] = {'attempted_at': runner.now.isoformat(), 'allocation_day': day,
                         'retry_after': (runner.now + timedelta(seconds=DRAFT_COOLDOWN_SECONDS)).isoformat()}
        runner.save()
        preparation_attempts += 1
        topics = policy.get('topics') or ['product design decisions']
        index = datetime.fromisoformat(day).date().toordinal() * len(slot_hours) + slot_hours.index(hour)
        context = {'topic': topics[index % len(topics)],
                   'background': policy.get('background', []),
                   'source_notes': policy.get('source_notes', []),
                   'recent_posts': [action['text'] for action in actions.values()
                                    if action.get('kind') == 'post' and isinstance(action.get('text'), str)][-3:]}
        if brief:
            context.update(topic=brief['topic'], content_brief=brief)
        source = None
        draft_task = task
        if design_news.requires_current_design(brief, policy, hour):
            source = design_news.select_source(runner, brief, publication_time=schedule)
            if source is None:
                print('Next-day post deferred: no unused fresh official design source is available.')
                continue
            design_news.apply_source_context(context, source)
            draft_task += '\n' + design_news.CURRENT_DESIGN_RULES
        asset = None
        if source is None and select_brief_format(policy, day, hour, brief) != 'text':
            try:
                asset = prepare_visual(runner, brief, day, hour, ROOT / 'testing' / 'visual-assets' / 'runtime')
            except apify_ai.ApifyBudgetError:
                runner.model_paused = True
                runner.state.setdefault('operational_status', {})['ai'] = 'budget-paused'
                continue
            text = asset['caption'] if asset else None
        else:
            text = runner.draft(draft_task, context)
        if source is not None:
            text = design_news.finalize_source_post(text, source, policy)
        if not text:
            continue
        if claimed_post_count(day, runner.state, zone_name) >= limit:
            continue
        kwargs = {'media_asset': asset} if asset else {}
        runner.write(key, 'post', text, scheduled_time=schedule, allocation_day=day, **kwargs)
        if key in runner.state['actions']:
            attempted.append(key)
    return attempted
