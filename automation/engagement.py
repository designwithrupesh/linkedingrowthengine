"""Durable, paced public engagement and bounded target caching.

These helpers make no network requests. Every prepared, rejected or uncertain
write intent consumes quota, and a target's explicit share/UGC identity remains
distinct from the activity URL used to read it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import html
import re
from typing import Any
from zoneinfo import ZoneInfo

from automation.discovery import post_urn, profile_handle


_UNRESOLVED = {'inflight', 'unknown-needs-reconciliation'}
_POST_FIELDS = ('url', 'urn', 'shareUrn', 'text', 'authorName', 'authorHeadline',
                'authorProfileUrl', 'postedAtISO', 'origin', 'sourceProfile')
_LINK = re.compile(r'https?://\S+', re.IGNORECASE)
_EXTERNAL_TEASER = re.compile(
    r'\b(?:full (?:post|article|episode|talk|video)|read (?:the )?(?:rest|full|more)'
    r"|watch (?:the )?(?:full|video|talk)|here['’]s the (?:talk|video|episode))\b", re.IGNORECASE)
_MEDIA_HEADLINE = re.compile(r'\b(?:on how to|on why|on what|(?:new|full) (?:video|talk|episode))\b', re.IGNORECASE)
_SPONSORED = re.compile(
    r'(?<!\w)#(?:ad|sponsored)\b|\b(?:paid partnership|sponsored (?:post|content|by)|advertisement)\b'
    r'|\b(?:thanks?|thank you) (?:to )?(?:our|the) (?:\w+ ){0,3}sponsors?\b', re.IGNORECASE)
_TEACHING_OFFER = re.compile(r'\b(?:workshop|course|cohort|masterclass|bootcamp|webinar|conference|tickets?|students?)\b', re.IGNORECASE)
_ENROLLMENT = re.compile(
    r'\b(?:enroll?ment|enroll?|register|registration|sign[ -]?up|early[ -]?bird'
    r'|(?:buy|book|reserve|get) (?:your |a )?(?:ticket|seat|spot)'
    r'|join (?:me|us)|next cohort|limited (?:seats|spots)|this (?:saturday|sunday)'
    r'|(?:final|last) reminder|(?:tickets?|seats?|spots?) (?:now )?(?:available|on sale))\b', re.IGNORECASE)
_DESIGN_DETAIL = re.compile(
    r'\b(?:users?|customers?|buttons?|forms?|inputs?|screens?|layouts?|errors?|prices?|checkout'
    r'|icons?|menus?|navigation|design|products?|brands?|prototypes?|typography|type|spacing'
    r'|headings?|labels?|contrast|colou?rs?|onboarding|sidebars?|canvas|undo|messages?|interfaces?)\b',
    re.IGNORECASE)
_ASSERTION = re.compile(
    r"\b(?:is|are|isn['’]t|aren['’]t|does|do|can|cannot|should|will|won['’]t|"
    r'(?:make|keep|need|add|hide|remove|stop|leave|let|block|feel|look|cost|cause|mean|help|'
    r'create|hurt|reduce|save|start|show|put|give|move|stay|work|read|fit)s?)\b', re.IGNORECASE)


def _clock(now: datetime, policy: dict) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Timezone-aware engagement clock required')
    return now.astimezone(ZoneInfo(policy.get('timezone', 'Asia/Calcutta')))


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _integer(value: Any, name: str, minimum: int = 0, maximum: int | None = None) -> int:
    if (not isinstance(value, int) or isinstance(value, bool) or value < minimum
            or (maximum is not None and value > maximum)):
        raise ValueError('Invalid engagement ' + name)
    return value


def public_action_counts(now: datetime, policy: dict, state: dict) -> dict:
    """Count today's public attempts separately from replies.

    Legacy ``interaction`` counters included replies. Their remainder remains
    authoritative when old receipts are missing; statuses never release quota.
    A new ``public_interaction`` counter, when present, already excludes replies.
    """
    local = _clock(now, policy)
    counts = {'reaction': 0, 'comment': 0, 'total': 0}
    replies = 0
    for action in state.get('actions', {}).values():
        if not isinstance(action, dict):
            continue
        stamp = _stamp(action.get('at'))
        day = action.get('allocation_day')
        if day is None and stamp is not None:
            day = stamp.astimezone(local.tzinfo).date().isoformat()
        if day != local.date().isoformat():
            continue
        kind = action.get('kind')
        if kind in ('reaction', 'comment'):
            counts[kind] += 1
        elif kind == 'reply':
            replies += 1
    bucket = state.get('days', {}).get(local.date().isoformat(), {})
    if 'public_interaction' in bucket:
        counter = _integer(bucket['public_interaction'], 'public counter')
    else:
        counter = max(0, _integer(bucket.get('interaction', 0), 'legacy counter') - replies)
    counts['total'] = max(counter, counts['reaction'] + counts['comment'])
    return counts


def public_actions_due(now: datetime, policy: dict, state: dict) -> int:
    """Return this slot's allowance without carrying missed work forward.

    Forty daily attempts are spread across twenty quarter-hour slots from
    17:00 through 21:45. Every write attempt in a slot consumes its allowance,
    including rejected and uncertain writes. Repeated five-minute jobs share
    that allowance through durable action timestamps.
    """
    local = _clock(now, policy)
    start = _integer(policy.get('public_engagement_start_hour', 17), 'start hour', maximum=23)
    end = _integer(policy.get('public_engagement_end_hour', 22), 'end hour', 1, 24)
    minutes = _integer(policy.get('public_engagement_slot_minutes', 15), 'slot minutes', 15, 60)
    target = _integer(policy.get('max_public_interactions_per_day', 40), 'daily target', maximum=40)
    session = _integer(policy.get('max_public_actions_per_session', 2), 'session limit', maximum=4)
    duration = (end - start) * 60
    if duration <= 0 or duration % minutes:
        raise ValueError('Invalid public engagement window')
    elapsed = local.hour * 60 + local.minute - start * 60
    if not 0 <= elapsed < duration:
        return 0
    slot = elapsed // minutes
    slots = duration // minutes
    accrued = target * (slot + 1) // slots
    allowance = accrued - target * slot // slots
    slot_start = local.replace(hour=start, minute=0, second=0, microsecond=0) + timedelta(minutes=slot * minutes)
    slot_end = slot_start + timedelta(minutes=minutes)
    attempted = 0
    for action in state.get('actions', {}).values():
        if not isinstance(action, dict) or action.get('kind') not in ('reaction', 'comment'):
            continue
        stamp = _stamp(action.get('at'))
        if stamp is not None and slot_start <= stamp < slot_end:
            attempted += 1
    total = public_action_counts(now, policy, state)['total']
    return max(0, min(session, allowance - attempted, accrued - total, target - total))


def canonical_target_urn(post: dict) -> str | None:
    """Prefer an explicit writable share/UGC identity without relabeling IDs."""
    if not isinstance(post, dict):
        return None
    candidates = [post_urn(post.get(field)) for field in ('shareUrn', 'urn', 'post_urn', 'url')]
    return (next((urn for urn in candidates if urn and urn.startswith(('urn:li:share:', 'urn:li:ugcPost:'))), None)
            or next((urn for urn in candidates if urn), None))


def _aliases(post: dict) -> set[str]:
    result = {urn for field in ('shareUrn', 'urn', 'post_urn', 'url')
              if (urn := post_urn(post.get(field)))}
    result.update(urn for value in post.get('identity_aliases', []) if (urn := post_urn(value)))
    return result


def _legacy_keys(post: dict) -> list[str]:
    urls = [post.get('url')]
    urls.extend('https://www.linkedin.com/feed/update/' + urn + '/' for urn in sorted(_aliases(post)))
    return list(dict.fromkeys('comment:' + hashlib.sha256(url.encode()).hexdigest()
                             for url in urls if isinstance(url, str) and post_urn(url)))


def comment_key(post: dict, state: dict) -> str:
    """Retain existing URL-hash intents; new writes hash the canonical URN."""
    urn = canonical_target_urn(post)
    if urn is None:
        raise ValueError('A valid public post identity is required')
    canonical = 'comment:' + hashlib.sha256(urn.encode()).hexdigest()
    actions = state.get('actions', {})
    aliases = _aliases(post)
    # Prefer a recorded comment over a reaction-only key if both exist.
    for key, action in actions.items():
        if (isinstance(action, dict) and action.get('kind') == 'comment'
                and action.get('post_urn') in aliases and key.startswith('comment:')):
            return key
    for key in [canonical] + _legacy_keys(post):
        if key in actions or 'reaction:' + key in actions:
            return key
    for key, action in actions.items():
        if (isinstance(action, dict) and action.get('kind') == 'reaction'
                and action.get('post_urn') in aliases and key.startswith('reaction:comment:')):
            return key[len('reaction:'):]
    return canonical


def _engagement(state: dict) -> dict:
    return state.setdefault('engagement', {})


def context_quality(post: dict) -> tuple[bool, str]:
    """Reject observed teaser/ad formats without guessing their missing content.

    This checks available text only. It neither follows links nor claims to
    understand a video, attachment or full article from its headline.
    """
    text = post.get('text') if isinstance(post, dict) else None
    if not isinstance(text, str) or not text.strip():
        return False, 'missing-context'
    text = html.unescape(text).strip()
    body = _LINK.sub('', text)
    body = re.sub(r'(?<!\w)#[\w-]+', '', body).strip()
    words = re.findall(r"[^\W_]+(?:['’][^\W_]+)?", body, re.UNICODE)
    if _SPONSORED.search(text) or (_TEACHING_OFFER.search(body) and _ENROLLMENT.search(body)):
        return False, 'promotion'
    if _LINK.search(text) and _EXTERNAL_TEASER.search(body) and len(words) < 35:
        return False, 'external-teaser'
    # An attributed title such as "Katie Dill on how to stop building zombie
    # UIs" gives the topic, not the speaker's argument or a video transcript.
    media = _MEDIA_HEADLINE.search(body)
    if len(words) < 35 and media:
        if not re.search(r'[.!?]["\')\]]?\s*$', body) or not _ASSERTION.search(body[:media.start()]):
            return False, 'media-headline'
    if len(words) < 12:
        # Keep a complete concise design assertion or a real question. Merely
        # ending a title with a full stop is not enough to supply its argument.
        complete = bool(re.search(r'[.!?]["\')\]]?\s*$', body))
        if re.search(r'\b(?:to discuss|to consider|should know|think about)\s*\.?$', body, re.IGNORECASE):
            complete = False
        specific = bool(_DESIGN_DETAIL.search(body) and _ASSERTION.search(body))
        if len(words) < 5 or not (complete and specific):
            return False, 'insufficient-context'
    return True, 'self-contained-context'


def _usable(now: datetime, policy: dict, post: dict) -> bool:
    if not isinstance(post, dict) or not canonical_target_urn(post) or not post_urn(post.get('url')):
        return False
    if not isinstance(post.get('text'), str) or not post['text'].strip():
        return False
    own = profile_handle(policy.get('profile_url'))
    if own and profile_handle(post.get('authorProfileUrl')) == own:
        return False
    max_age = _integer(policy.get('target_post_max_age_days', 7), 'target age', 1, 7)
    published = _stamp(post.get('postedAtISO')) or _stamp(post.get('first_seen_at'))
    return published is None or now.astimezone(timezone.utc) - published <= timedelta(days=max_age)


def stash_targets(now: datetime, policy: dict, state: dict, posts: list,
                  source_profile: str | None = None) -> None:
    """Merge at most one hundred safe normalized targets into durable state."""
    _clock(now, policy)
    cache = [dict(post) for post in _engagement(state).get('targets', []) if _usable(now, policy, post)]
    stamp = now.astimezone(timezone.utc).isoformat()
    for post in posts[:100]:
        if not _usable(now, policy, post):
            continue
        record = {key: post[key] for key in _POST_FIELDS if key in post}
        record['urn'] = canonical_target_urn(post)
        record['text'] = record['text'][:20000]
        aliases = _aliases(post)
        previous = next((item for item in cache if _aliases(item) & aliases), None)
        if previous is not None:
            aliases |= _aliases(previous)
            cache.remove(previous)
            # An activity-only refresh must not discard a prior explicit share.
            if record['urn'].startswith('urn:li:activity:'):
                record['urn'] = canonical_target_urn(previous)
                record['shareUrn'] = previous.get('shareUrn')
        record['identity_aliases'] = sorted(aliases)
        record['first_seen_at'] = previous.get('first_seen_at', stamp) if previous else stamp
        record['cached_at'] = stamp
        cache.append(record)
    _engagement(state)['targets'] = cache[-100:]
    if source_profile:
        mark_discovered(now, policy, state, source_profile)


def candidate_targets(now: datetime, policy: dict, state: dict) -> list[dict]:
    """Return contextual targets with remaining work before any model spend."""
    _clock(now, policy)
    actions = state.get('actions', {})
    deferred = _engagement(state).get('deferred_until', {})
    result = []
    seen = set()
    for post in _engagement(state).get('targets', []):
        if not _usable(now, policy, post):
            continue
        eligible, reason = context_quality(post)
        # Stable fixed diagnostics survive normal state checkpoints without
        # creating write intents, quota usage, or a new timestamp every poll.
        post['quality_decision'] = {'eligible': eligible, 'reason': reason}
        if not eligible:
            continue
        aliases = _aliases(post)
        if aliases & seen:
            continue
        seen |= aliases
        until = next((stamp for alias in aliases if (stamp := _stamp(deferred.get(alias)))
                      and stamp > now.astimezone(timezone.utc)), None)
        if until:
            continue
        keys = [comment_key(post, state)] + _legacy_keys(post)
        if any(isinstance(action, dict) and action.get('kind') == 'reaction'
               and action.get('status') in _UNRESOLVED
               and (action.get('post_urn') in aliases or key in ['reaction:' + item for item in keys])
               for key, action in actions.items()):
            continue
        comment_done = any(key in actions for key in keys)
        reaction_done = any('reaction:' + key in actions for key in keys)
        # Receipts on another URL spelling still establish completed attempts.
        comment_done |= any(isinstance(action, dict) and action.get('kind') == 'comment'
                            and action.get('post_urn') in aliases for action in actions.values())
        reaction_done |= any(isinstance(action, dict) and action.get('kind') == 'reaction'
                             and action.get('post_urn') in aliases for action in actions.values())
        if not comment_done or (policy.get('react_to_target_posts', True) and not reaction_done):
            result.append(post)
    return result


def mark_deferred(now: datetime, state: dict, post: dict, hours: int = 24) -> None:
    """Avoid paying to regenerate the same unsuitable post every five minutes."""
    _clock(now, {})
    hours = _integer(hours, 'deferral hours', 1, 168)
    urn = canonical_target_urn(post)
    if urn is None:
        raise ValueError('A valid public post identity is required')
    until = (now.astimezone(timezone.utc) + timedelta(hours=hours)).isoformat()
    deferred = _engagement(state).setdefault('deferred_until', {})
    for alias in _aliases(post):
        deferred[alias] = until
    # Only retained target identities need persistent deferrals.
    retained = {alias for item in _engagement(state).get('targets', []) for alias in _aliases(item)}
    _engagement(state)['deferred_until'] = {alias: value for alias, value in deferred.items()
                                           if alias in retained or alias in _aliases(post)}


def next_discovery_profile(policy: dict, state: dict) -> str | None:
    """Match the existing discover_targets cursor and owner exclusion exactly."""
    own = profile_handle(policy.get('profile_url'))
    sources = list(dict.fromkeys(handle for value in policy.get('discovery_profiles', [])
                                if (handle := profile_handle(value)) and handle != own))
    if not sources:
        return None
    cursor = state.get('discovery', {}).get('profile_cursor', 0)
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        cursor = 0
    return 'https://www.linkedin.com/in/' + sources[cursor % len(sources)] + '/'


def should_discover(now: datetime, policy: dict, state: dict,
                    source_profile: str | None = None) -> bool:
    """A profile is scraped at most once per six hours across short runs."""
    _clock(now, policy)
    handle = profile_handle(source_profile or next_discovery_profile(policy, state))
    if not handle or handle == profile_handle(policy.get('profile_url')):
        return False
    ttl = _integer(policy.get('target_cache_ttl_hours', 6), 'cache TTL', 1, 24)
    stamp = _stamp(_engagement(state).get('profile_checked_at', {}).get(handle))
    return stamp is None or now.astimezone(timezone.utc) - stamp >= timedelta(hours=ttl)


def mark_discovered(now: datetime, policy: dict, state: dict, source_profile: str) -> None:
    """Record a profile scrape attempt before its checkpointed paid request."""
    _clock(now, policy)
    handle = profile_handle(source_profile)
    if not handle or handle == profile_handle(policy.get('profile_url')):
        raise ValueError('A valid external source profile is required')
    _engagement(state).setdefault('profile_checked_at', {})[handle] = now.astimezone(timezone.utc).isoformat()
