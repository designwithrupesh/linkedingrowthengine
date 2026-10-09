"""Bounded batch reads with explicit post identity and thread coverage.

The public actor accepts multiple post URLs and a limit per post. Its current
input schema also exposes ``page_number``. Returned ``post_input`` and
``comment_url`` fields associate each row with its requested post; a bare
numeric ID is never converted from activity to share or ugcPost.
"""
from __future__ import annotations

from collections import defaultdict
import re
from urllib.parse import unquote, urlsplit

from lib.url_parser import parse_linkedin_url

POST_URN = re.compile(r'urn:li:(?:activity|share|ugcPost):\d{18,25}')
NUMERIC_ID = re.compile(r'\d{18,25}')
MAX_POSTS = 5
MAX_ROWS = 5000


def _public_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').casefold().removeprefix('www.')
        if (parsed.scheme != 'https' or host != 'linkedin.com' or parsed.username
                or parsed.password or parsed.port not in (None, 443)):
            return None
        return 'https://linkedin.com' + unquote(parsed.path).rstrip('/')
    except ValueError:
        return None


def _identity(value, *, numeric=False):
    if not isinstance(value, str):
        return set()
    value = value.strip()
    if POST_URN.fullmatch(value):
        return {value}
    public = _public_url(value)
    if public:
        urn = parse_linkedin_url(value).get('post_urn')
        return {public, urn} if urn else {public}
    # Numeric post_input is an evidenced actor output. Its exact digits must
    # already be an explicit identifier of a known input or post URN.
    return {value} if numeric and NUMERIC_ID.fullmatch(value) else set()


def _targets(posts):
    if not isinstance(posts, (list, tuple)) or not 1 <= len(posts) <= MAX_POSTS:
        raise ValueError('Comment batches require 1 to 5 owned posts')
    result, aliases = {}, defaultdict(set)
    for post in posts:
        urn = post.get('post_urn') if isinstance(post, dict) else None
        if not isinstance(urn, str) or not POST_URN.fullmatch(urn):
            raise ValueError('Comment batches require verified post URNs')
        if urn in result:
            raise ValueError('Comment batches require distinct owned posts')
        result[urn] = post
        known = {urn}
        for field in ('url', 'read_url'):
            if post.get(field):
                if not _public_url(post[field]) or not parse_linkedin_url(post[field]).get('post_urn'):
                    raise ValueError('Comment batches require verified public LinkedIn read URLs')
                known.update(_identity(post[field]))
        for identity in known:
            aliases[identity].add(urn)
            if POST_URN.fullmatch(identity):
                # Matching this exact known identifier creates no new URN and
                # preserves ambiguity when two known posts share the digits.
                aliases[identity.rsplit(':', 1)[1]].add(urn)
    return result, aliases


def _matches(row, aliases):
    matches, identified = set(), False
    for field in ('post_input', 'post_urn', 'postUrn', 'post_url', 'postUrl', 'comment_url'):
        identities = _identity(row.get(field), numeric=field == 'post_input')
        identified = identified or bool(identities)
        for identity in identities:
            matches.update(aliases.get(identity, ()))
    # post_id is safe only when it is a complete typed URN or public URL.
    # A numeric value alone has no evidenced activity/share/ugcPost type.
    identities = _identity(row.get('post_id'))
    identified = identified or bool(identities)
    for identity in identities:
        matches.update(aliases.get(identity, ()))
    return matches, identified


def _safe_children(row, urn, aliases, stats, depth=0):
    cloned = dict(row)
    children = row.get('replies')
    if isinstance(children, list) and depth < 20:
        safe = []
        for child in children:
            if not isinstance(child, dict):
                continue
            matched, _ = _matches(child, aliases)
            if matched and matched != {urn}:
                stats['conflicting_nested'] += 1
                continue
            safe.append(_safe_children(child, urn, aliases, stats, depth + 1))
        cloned['replies'] = safe
    elif children:
        cloned['replies'] = []
        stats['truncated_nested'] += 1
    return cloned


def _threads(rows):
    """Attach flat replies only when their top parent's context is present."""
    nodes, nested_top = {}, {}

    def visit(row, top_id, depth=0):
        if not isinstance(row, dict) or depth > 20:
            return
        cid = str(row.get('comment_id') or '')
        if cid:
            nodes.setdefault(cid, row)
            if cid != top_id:
                nested_top[cid] = top_id
        for child in row.get('replies') or []:
            visit(child, top_id, depth + 1)

    roots, flat, seen = {}, [], set()
    for row in rows:
        cid = str(row.get('comment_id') or '')
        if not cid or cid in seen:
            continue
        seen.add(cid)
        visit(row, cid)
        if row.get('parent_comment_id') or row.get('comment_type') == 'reply':
            flat.append(row)
        else:
            roots[cid] = {**row, 'replies': list(row.get('replies') or [])}

    unresolved = 0
    for row in flat:
        cid = str(row['comment_id'])
        if cid in nested_top:
            continue
        parent = str(row.get('parent_comment_id') or '')
        chain = {cid}
        for _ in range(21):
            if parent in nested_top:
                parent = nested_top[parent]
            if parent in roots:
                roots[parent]['replies'].append(row)
                break
            if parent in chain or parent not in nodes:
                unresolved += 1
                break
            chain.add(parent)
            parent = str(nodes[parent].get('parent_comment_id') or '')
        else:
            unresolved += 1
    return list(roots.values()), unresolved


def group_comments(rows, posts, *, max_items=100, page_number=1):
    """Return safely attributed thread rows and explicit coverage diagnostics."""
    targets, aliases = _targets(posts)
    if not isinstance(rows, list):
        raise ValueError('Comment actor results must be a list')
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 100:
        raise ValueError('Comment result limits must be between 1 and 100')
    if not isinstance(page_number, int) or isinstance(page_number, bool) or not 1 <= page_number <= 100:
        raise ValueError('Comment page numbers must be between 1 and 100')
    grouped = {urn: [] for urn in targets}
    stats = {'unmapped': 0, 'conflicting': 0, 'summaries': 0, 'invalid': 0,
             'conflicting_nested': 0, 'truncated_nested': 0,
             'truncated_rows': max(0, len(rows) - MAX_ROWS)}
    for row in rows[:MAX_ROWS]:
        if not isinstance(row, dict):
            stats['invalid'] += 1
            continue
        if 'summary' in row:
            stats['summaries'] += 1
            continue
        if not row.get('comment_id'):
            stats['invalid'] += 1
            continue
        matched, identified = _matches(row, aliases)
        if len(matched) > 1:
            stats['conflicting'] += 1
            continue
        if not matched:
            if len(targets) == 1 and not identified:
                matched = set(targets)
            else:
                stats['unmapped'] += 1
                continue
        urn = next(iter(matched))
        grouped[urn].append(_safe_children(row, urn, aliases, stats))
    coverage = {}
    for urn, raw in grouped.items():
        threads, unresolved = _threads(raw)
        grouped[urn] = threads
        totals = [row.get('totalComments') for row in raw]
        total = max((item for item in totals if isinstance(item, int) and not isinstance(item, bool)), default=0)
        saturated = len(threads) >= max_items or total > page_number * max_items
        attribution_uncertain = bool(stats['unmapped'] or stats['conflicting'] or stats['truncated_rows'])
        coverage[urn] = {'returned_rows': len(raw), 'returned_top_level': len(threads),
                         'unresolved_replies': unresolved, 'page_number': page_number,
                         'coverage': 'identity-unmapped' if attribution_uncertain else 'thread-parent-missing'
                         if unresolved else 'actor-result-limit' if saturated else 'bounded-public-results',
                         'next_page': page_number + 1 if saturated and page_number < 100 else None}
    return {'posts': grouped, 'coverage': coverage, **stats}


def fetch_comments(reader, posts, *, max_items=100, page_number=1, input_format='url'):
    """Run one fresh actor batch, with at most five inputs and 100 rows per post.

    URLs are the documented actor input. ``input_format='urn'`` is retained for
    callers already using typed URNs; association remains exact in either case.
    """
    targets, _ = _targets(posts)
    if input_format not in ('url', 'urn'):
        raise ValueError('Comment batch inputs must use verified URLs or URNs')
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 100:
        raise ValueError('Comment result limits must be between 1 and 100')
    if not isinstance(page_number, int) or isinstance(page_number, bool) or not 1 <= page_number <= 100:
        raise ValueError('Comment page numbers must be between 1 and 100')
    values = []
    for urn, post in targets.items():
        if input_format == 'url':
            value = post.get('read_url') or post.get('url')
            if not value:
                raise ValueError('URL comment batches require verified public read URLs')
        else:
            value = urn
        values.append(value)
    # The actor's newest-first results can omit existing nested replies.
    # Relevant ordering returns thread coverage; pagination still advances
    # through all available top-level comments on busy owned posts.
    payload = {'postIds': values, 'limit': max_items, 'sortOrder': 'most relevant'}
    if page_number != 1:
        payload['page_number'] = page_number
    rows = reader._run_sync(reader.POST_COMMENTS_ACTOR, payload, force_refresh=True)
    return group_comments(rows, posts, max_items=max_items, page_number=page_number)
