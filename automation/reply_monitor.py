"""Durable, bounded reply discovery without network calls or remote writes.

Polling and publishing stay in the runner. This module keeps each observed
comment in a queue until a durable write intent exists, so public engagement
cannot consume its quota or erase it between workflow runs. Public scraper
results can be incomplete; coverage metadata never claims every comment was
returned by the provider.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from urllib.parse import unquote, urlsplit

from lib.url_parser import build_parent_comment_urn, parse_linkedin_url

POST_URN = re.compile(r'urn:li:(?:activity|share|ugcPost):\d{18,25}')
COMMENT_ID = re.compile(r'\d{1,25}')
MAX_PENDING = 1000


def _clock(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Reply monitoring requires a timezone-aware clock')
    return now.astimezone(timezone.utc)


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        if parsed is not None and parsed.tzinfo is not None:
            return parsed.timestamp()
    except ValueError:
        pass
    return 0


def _monitor(state):
    monitor = state.setdefault('reply_monitor', {})
    monitor.setdefault('posts', {})
    monitor.setdefault('pending', {})
    return monitor


def _profile_identity(url):
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').casefold().removeprefix('www.')
        path = unquote(parsed.path).rstrip('/').casefold()
        if parsed.scheme not in ('https', 'http') or host != 'linkedin.com' or not path.startswith('/in/'):
            return None
        if not path.removeprefix('/in/') or '/' in path.removeprefix('/in/'):
            return None
        return path
    except ValueError:
        return None


def published_targets(state, max_posts=10):
    """Return the newest bounded set of owned posts with verified public URLs.

    A separately recorded canonical post URN takes precedence over the activity
    URL. The digits of an activity ID are never relabelled as a share ID.
    """
    if not isinstance(max_posts, int) or isinstance(max_posts, bool) or not 1 <= max_posts <= 100:
        raise ValueError('Reply monitoring supports 1 to 100 owned posts')
    result, seen = [], set()
    for post in reversed(state.get('posts', [])):
        if not isinstance(post, dict) or not isinstance(post.get('url'), str):
            continue
        try:
            parsed = urlsplit(post['url'])
            if parsed.scheme != 'https' or (parsed.hostname or '').casefold().removeprefix('www.') != 'linkedin.com':
                continue
            from_url = parse_linkedin_url(post['url']).get('post_urn')
        except ValueError:
            continue
        if not from_url:
            continue
        urn = post.get('post_urn') or post.get('urn') or from_url
        if not isinstance(urn, str) or not POST_URN.fullmatch(urn) or urn in seen:
            continue
        seen.add(urn)
        result.append({**post, 'post_urn': urn})
        if len(result) == max_posts:
            break
    return result


def poll_targets(state, now, *, max_posts=10, limit=1, min_interval_seconds=300):
    """Choose least recently attempted posts, retaining rotation across runs."""
    now = _clock(now)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError('Poll limit must be a non-negative integer')
    if not isinstance(min_interval_seconds, (int, float)) or min_interval_seconds < 0:
        raise ValueError('Poll interval must be non-negative')
    monitor = _monitor(state)
    eligible = []
    for rank, post in enumerate(published_targets(state, max_posts)):
        previous = monitor['posts'].get(post['post_urn'], {})
        last = _timestamp(previous.get('last_attempt_at'))
        if last and now.timestamp() - last < min_interval_seconds:
            continue
        eligible.append((last, rank, post))
    return [item[2] for item in sorted(eligible, key=lambda item: item[:2])[:limit]]


def record_poll(state, post, now, *, rows=None, error=False, max_items=100):
    """Record an attempted read and whether its bounded coverage is saturated.

    Failure also advances rotation, preventing one inaccessible post from
    starving other posts. No raw exception or provider response is stored.
    """
    now = _clock(now)
    urn = post['post_urn']
    record = _monitor(state)['posts'].setdefault(urn, {})
    record['last_attempt_at'] = now.isoformat()
    record['poll_count'] = record.get('poll_count', 0) + 1
    record['status'] = 'read-failed' if error else 'checked' if rows is not None else 'attempted'
    if rows is not None and not error:
        comments = [row for row in rows if isinstance(row, dict) and row.get('comment_id')]
        totals = [row.get('totalComments') for row in comments]
        known_total = max((value for value in totals if isinstance(value, int) and not isinstance(value, bool)), default=0)
        limited = len(comments) >= max_items or known_total > len(comments)
        record.update(last_success_at=now.isoformat(), returned_top_level=len(comments),
                      coverage='actor-result-limit' if limited else 'bounded-public-results')
    return record


def _comments(rows, own_url):
    """Flatten nested threads while preserving the top-level parent context."""
    own = _profile_identity(own_url)
    if own is None:
        raise ValueError('A verified owner profile URL is required to monitor replies')
    result, counts, visited = [], {'self': 0, 'unidentified': 0}, set()

    def walk(row, parent_id, parent_text, parent_author, depth=0):
        if not isinstance(row, dict) or depth > 20 or len(visited) >= 5000 or id(row) in visited:
            return
        visited.add(id(row))
        cid = str(row.get('comment_id') or '')
        author = row.get('author') if isinstance(row.get('author'), dict) else {}
        identity = _profile_identity(author.get('profile_url'))
        if COMMENT_ID.fullmatch(cid):
            if identity == own:
                counts['self'] += 1
            elif identity is None:
                # Missing author identity cannot establish that this is not our
                # own reply. Keep it out of automatic writes.
                counts['unidentified'] += 1
            elif isinstance(row.get('text'), str) and row['text'].strip():
                posted = row.get('posted_at') if isinstance(row.get('posted_at'), dict) else {}
                result.append({'id': cid, 'parent_id': parent_id, 'text': row['text'][:4000],
                               'author': str(author.get('name') or '')[:300],
                               'author_profile_url': author['profile_url'], 'timestamp': posted.get('timestamp'),
                               'parent_text': parent_text[:4000], 'parent_author': parent_author[:300]})
        for child in row.get('replies') or []:
            walk(child, parent_id, parent_text, parent_author, depth + 1)

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get('comment_id') or '')
        if not COMMENT_ID.fullmatch(cid):
            continue
        author = row.get('author') if isinstance(row.get('author'), dict) else {}
        walk(row, cid, str(row.get('text') or ''), str(author.get('name') or ''))
    return result, counts


def queue_comments(state, post, rows, own_url, now, *, max_pending=MAX_PENDING):
    """Keep each newly observed comment durable, including old or undated ones.

    Existing write intents always win, including uncertain outcomes. An
    observed comment is never retried merely because a read found it again.
    Queue overflow is recorded explicitly instead of claiming full coverage.
    """
    now = _clock(now)
    if not isinstance(max_pending, int) or isinstance(max_pending, bool) or not 1 <= max_pending <= MAX_PENDING:
        raise ValueError('Pending reply capacity must be between 1 and 1000')
    urn = post['post_urn']
    if not isinstance(urn, str) or not POST_URN.fullmatch(urn):
        raise ValueError('A verified post URN is required to queue replies')
    monitor = _monitor(state)
    pending_replies(state)  # Remove comments for which an intent already exists.
    comments, stats = _comments(rows, own_url)
    stats.update(queued=0, duplicate=0, overflow=0)
    for comment in comments:
        key = 'reply:' + urn + ':' + comment['id']
        if key in state.get('actions', {}):
            stats['duplicate'] += 1
            continue
        if key in monitor['pending']:
            # Edited source comments can update context without losing their
            # place in the queue or removing a generation deferral.
            monitor['pending'][key]['comment'] = comment
            stats['duplicate'] += 1
            continue
        if len(monitor['pending']) >= max_pending:
            stats['overflow'] += 1
            continue
        monitor['pending'][key] = {
            'key': key, 'post_urn': urn, 'post_url': post['url'],
            'post_text': str(post.get('text') or '')[:3000], 'comment': comment,
            'parent': build_parent_comment_urn(urn, comment['parent_id']),
            'first_seen_at': now.isoformat(), 'generation_deferrals': 0,
        }
        stats['queued'] += 1
    monitor['last_ingest'] = {'at': now.isoformat(), 'post_urn': urn, **stats}
    return stats


def pending_replies(state, *, now=None, limit=None):
    """Return eligible queued replies in order, pruning all established intents."""
    if now is not None:
        now = _clock(now)
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        raise ValueError('Reply limit must be a non-negative integer')
    pending = _monitor(state)['pending']
    for key in list(pending):
        if key in state.get('actions', {}):
            del pending[key]
    result = [entry for entry in pending.values()
              if now is None or _timestamp(entry.get('eligible_after')) <= now.timestamp()]
    result.sort(key=lambda entry: (_timestamp(entry.get('first_seen_at')), entry['key']))
    return result if limit is None else result[:limit]


def defer_reply(state, key, now, *, seconds=900, reason='generation-deferred'):
    """Back off a skipped generation without consuming a remote-write intent."""
    now = _clock(now)
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 <= seconds <= 86400:
        raise ValueError('Reply deferrals must be between 0 and 86400 seconds')
    entry = _monitor(state)['pending'].get(key)
    if entry is None:
        return
    entry['eligible_after'] = (now + timedelta(seconds=seconds)).isoformat()
    entry['generation_deferrals'] = entry.get('generation_deferrals', 0) + 1
    # Reasons are caller-controlled fixed diagnostics, not external model text.
    entry['deferred_reason'] = reason if reason in ('generation-deferred', 'budget-deferred') else 'generation-deferred'
