"""One-shot draft, signed upload, validation and scheduling for LinkedIn media.

The caller owns the post quota and first durable action intent. A held operation
never falls back to a text post or creates another draft on a later invocation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
import re
import struct
from time import monotonic
from urllib.parse import urlsplit

import requests

from lib.voice import require_plain_text


BASE_URL = 'https://api.publora.com/api/v1'
_IDENTIFIER = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_S3_HOST = re.compile(
    r'^(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com$'
)


class MediaPostCheckpointError(RuntimeError):
    """A durable checkpoint failed; no following request may be attempted."""


class MediaPostOutcomeError(RuntimeError):
    """A media request has an uncertain remote outcome."""


def valid_upload_url(value):
    """Allow the documented HTTPS S3 upload hosts, without embedded credentials."""
    if not isinstance(value, str) or len(value) > 16384 or any(ord(c) < 33 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == 'https' and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443) and not parsed.fragment and bool(parsed.path.strip('/'))
                and bool(_S3_HOST.fullmatch(parsed.hostname or '')))
    except ValueError:
        return False


def _identifier(value):
    return isinstance(value, str) and bool(_IDENTIFIER.fullmatch(value))


def _checkpoint(runner):
    try:
        runner.save()
    except Exception:
        # Never include provider URLs, bodies or credential-bearing exceptions.
        raise MediaPostCheckpointError(
            'Saving the media post checkpoint failed. This action must not be retried.'
        ) from None


def _stage(runner, action, name):
    action['media_stage'] = name
    action['media_stage_status'] = 'inflight'
    _checkpoint(runner)


def _hold(runner, action, reason, *, uncertain=False, status=None):
    action.update(status='unknown-needs-reconciliation' if uncertain else 'media-held',
                  reason=reason, media_stage_status='unknown' if uncertain else 'held')
    if status is not None:
        action['http_status'] = status
    _checkpoint(runner)
    if uncertain:
        raise MediaPostOutcomeError(
            'Media post outcome is uncertain. Reconcile the existing draft before any retry.'
        ) from None
    return None


def _api(runner, action, method, endpoint, *, payload=None):
    """One request, no redirects or retries, and no external error text."""
    headers = {'x-publora-key': os.environ['PUBLORA_API_KEY']}
    if method in ('post', 'put') and endpoint.startswith(('/create-post', '/update-post/')):
        # The provider documents idempotency only on draft creation and edits.
        seed = action['media_operation_id'] + ':' + action['media_stage']
        headers['Idempotency-Key'] = hashlib.sha256(seed.encode()).hexdigest()
    kwargs = {'headers': headers, 'timeout': 45, 'allow_redirects': False}
    if payload is not None:
        kwargs['json'] = payload
    try:
        response = getattr(requests, method)(BASE_URL + endpoint, **kwargs)
    except Exception:
        return _hold(runner, action, 'Media transport acknowledgement unavailable', uncertain=True)
    status = response.status_code
    action['http_status'] = status
    if not 200 <= status < 300:
        uncertain = not (400 <= status < 500 and status not in (408, 429))
        if not uncertain and action['media_stage'] == 'create-draft':
            action.update(status='rejected', reason='Publora rejected the media draft',
                          media_stage_status='rejected')
            _checkpoint(runner)
            return None
        return _hold(runner, action, 'Publora did not acknowledge the media stage',
                     uncertain=uncertain, status=status)
    try:
        data = response.json()
    except Exception:
        return _hold(runner, action, 'Media stage response could not be verified', uncertain=True)
    if not isinstance(data, dict) or data.get('success') is not True:
        return _hold(runner, action, 'Media stage response did not establish success', uncertain=True)
    return data


def _asset(asset):
    """Verify the exact local bytes before creating a remote draft."""
    if not isinstance(asset, dict):
        raise ValueError('A rendered media asset is required')
    path = Path(asset['path'])
    mime = asset.get('mime_type')
    format_kind = asset.get('kind')
    if format_kind not in ('image', 'carousel', 'freebie'):
        raise ValueError('An image, carousel or freebie format is required')
    if mime not in ('image/png', 'application/pdf'):
        raise ValueError('Only rendered PNG and PDF assets are supported')
    if (format_kind == 'image') != (mime == 'image/png'):
        raise ValueError('Rendered MIME does not match its selected format')
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if not data or digest != asset.get('sha256'):
        raise ValueError('Rendered media checksum does not match')
    title = require_plain_text(asset.get('title')).strip()
    if not title or len(title) > 180:
        raise ValueError('Media title must contain readable text')
    if mime == 'image/png':
        if len(data) > 50 * 1024 * 1024 or data[:8] != b'\x89PNG\r\n\x1a\n' or len(data) < 24:
            raise ValueError('PNG asset exceeds the supported bounds')
        width, height = struct.unpack('>II', data[16:24])
        if width <= 0 or height <= 0 or width * height > 36152320:
            raise ValueError('PNG dimensions exceed the supported bounds')
        from PIL import Image
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != 'PNG' or image.size != (width, height):
                    raise ValueError('Rendered PNG metadata does not match its bytes')
                image.verify()
        except Exception:
            raise ValueError('Rendered PNG could not be verified') from None
        kind, extension = 'image', '.png'
    else:
        pages = asset.get('page_count')
        if (len(data) > 100 * 1024 * 1024 or not data.startswith(b'%PDF-')
                or isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 300):
            raise ValueError('PDF asset exceeds the supported bounds')
        kind, extension = 'document', '.pdf'
    filename = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:96] or 'design-resource'
    return data, {'sha256': digest, 'mime_type': mime, 'title': title,
                  'type': kind, 'page_count': asset.get('page_count'),
                  'format': asset.get('kind'), 'file_name': filename + extension,
                  'size_bytes': len(data)}


def validate_media_asset(asset):
    """Public validation for the caller to run before reserving post quota."""
    return _asset(asset)[1]


def _group(data):
    group = data.get('postGroup') or data
    return group if isinstance(group, dict) else {}


def _verified_group(data, runner, action, media_id, *, scheduled=False):
    group = _group(data)
    group_id = group.get('postGroupId') or group.get('_id') or group.get('id')
    if group_id is not None and str(group_id) != action['remote_id']:
        return False
    if group.get('platforms') != [runner.policy['platform_id']]:
        return False
    media = group.get('media')
    if not isinstance(media, list) or len(media) != 1 or not isinstance(media[0], dict):
        return False
    item = media[0]
    if (str(item.get('mediaId') or item.get('_id') or '') != media_id
            or item.get('status') != 'ready' or item.get('failureReason')
            or item.get('type') != action['media']['type']
            or item.get('mimeType') != action['media']['mime_type']):
        return False
    content = group.get('content')
    if content is not None and content != action['text']:
        return False
    children = [child for child in group.get('posts', [])
                if isinstance(child, dict) and child.get('platform') == 'linkedin']
    if not children or any(child.get('content') != action['text'] for child in children):
        return False
    if scheduled:
        if group.get('status') != 'scheduled':
            return False
        try:
            actual = datetime.fromisoformat(group['scheduledTime'].replace('Z', '+00:00'))
            intended = datetime.fromisoformat(action['media_scheduled_for'].replace('Z', '+00:00'))
        except (KeyError, ValueError, TypeError, AttributeError):
            return False
        if actual.tzinfo is None or actual != intended:
            return False
    elif group.get('status') != 'draft':
        return False
    return True


def publish_media_post(runner, action, asset):
    """Complete an already checkpointed new media intent exactly once."""
    if action.get('status') != 'inflight' or action.get('remote_id') or action.get('media_stage'):
        return action
    pipeline_start = monotonic()
    data, metadata = _asset(asset)
    require_plain_text(action['text'])
    schedule = datetime.fromisoformat(action['scheduled_for'].replace('Z', '+00:00'))
    if schedule.tzinfo is None or schedule <= runner.now:
        raise ValueError('Media posts require a future aware schedule')
    action['media'] = metadata
    action['media_scheduled_for'] = schedule.astimezone(timezone.utc).replace(microsecond=0).strftime('%Y-%m-%dT%H:%M:%SZ')
    seed = action['at'] + ':' + action['scheduled_for'] + ':' + metadata['sha256'] + ':' + action['text']
    action['media_operation_id'] = hashlib.sha256(seed.encode()).hexdigest()
    _stage(runner, action, 'create-draft')
    response = _api(runner, action, 'post', '/create-post',
                    payload={'content': action['text'], 'platforms': [runner.policy['platform_id']]})
    if response is None:
        return action
    remote_id = response.get('postGroupId')
    if not _identifier(remote_id):
        _hold(runner, action, 'Draft acknowledgement did not include a valid identifier', uncertain=True)
    action.update(remote_id=remote_id, media_stage_status='acknowledged')
    record = {'id': remote_id, 'text': action['text'], 'url': None, 'at': action['at'],
              'scheduled_for': action['scheduled_for'], 'allocation_day': action['allocation_day'],
              'media_status': 'draft', 'format': metadata['format'], 'asset_sha256': metadata['sha256']}
    runner.state['posts'].append(record)
    _checkpoint(runner)

    _stage(runner, action, 'reserve-upload')
    response = _api(runner, action, 'post', '/get-upload-url', payload={
        'fileName': metadata['file_name'], 'contentType': metadata['mime_type'],
        'type': metadata['type'], 'postGroupId': remote_id})
    if response is None:
        return action
    media_id, upload_url = response.get('mediaId'), response.get('uploadUrl')
    if not _identifier(media_id):
        _hold(runner, action, 'Upload reservation identifier could not be verified', uncertain=True)
    action['media']['media_id'] = media_id
    action['media_stage_status'] = 'acknowledged'
    _checkpoint(runner)  # Persist reservation before validating or using its URL.
    if not valid_upload_url(upload_url):
        _hold(runner, action, 'Upload destination did not match documented storage hosts')
        return action
    _stage(runner, action, 'upload-bytes')
    try:
        uploaded = requests.put(upload_url, headers={'Content-Type': metadata['mime_type']},
                                data=data, timeout=90, allow_redirects=False)
    except Exception:
        _hold(runner, action, 'Storage upload acknowledgement unavailable', uncertain=True)
    if not 200 <= uploaded.status_code < 300:
        _hold(runner, action, 'Storage upload was not acknowledged',
              uncertain=not (400 <= uploaded.status_code < 500 and uploaded.status_code not in (408, 429)),
              status=uploaded.status_code)
        return action
    action['media_stage_status'] = 'acknowledged'
    _checkpoint(runner)

    _stage(runner, action, 'complete-media')
    response = _api(runner, action, 'post', '/complete-media/' + media_id)
    if response is None:
        return action
    completed = response.get('mediaFile')
    if (not isinstance(completed, dict) or str(completed.get('_id') or '') != media_id
            or completed.get('status') in ('failed', 'invalid')):
        _hold(runner, action, 'Media completion could not be verified')
        return action
    action['media_stage_status'] = 'acknowledged'
    _checkpoint(runner)
    action['media_stage'] = 'verify-attachment'
    response = _api(runner, action, 'get', '/get-post/' + remote_id)
    if response is None:
        return action
    if not _verified_group(response, runner, action, media_id):
        _hold(runner, action, 'Draft attachment, caption or channel could not be verified')
        return action
    action['media']['status'] = 'ready'
    record['media_status'] = 'ready'
    _checkpoint(runner)

    current = runner.now + timedelta(seconds=max(0, monotonic() - pipeline_start))
    publication_time = datetime.fromisoformat(action['media_scheduled_for'].replace('Z', '+00:00'))
    if publication_time <= current:
        _hold(runner, action, 'Publication time elapsed while preparing the media draft')
        return action
    _stage(runner, action, 'schedule-media')
    response = _api(runner, action, 'put', '/update-post/' + remote_id,
                    payload={'status': 'scheduled', 'scheduledTime': action['media_scheduled_for']})
    if response is None:
        return action
    action['media_stage_status'] = 'acknowledged'
    record['media_status'] = 'schedule-acknowledged'
    _checkpoint(runner)
    action['media_stage'] = 'verify-schedule'
    response = _api(runner, action, 'get', '/get-post/' + remote_id)
    if response is None:
        return action
    if not _verified_group(response, runner, action, media_id, scheduled=True):
        _hold(runner, action, 'Scheduled media, caption, channel or time could not be verified')
        return action
    action.update(status='scheduled', media_stage_status='verified',
                  reason='Publora confirmed scheduled media and caption')
    record['media_status'] = 'scheduled'
    _checkpoint(runner)
    return action


def _replacement_group(data, runner, audit, *, media_ids, caption, scheduled=False):
    """Verify account, caption and exact media inventory without persisting URLs."""
    group = _group(data)
    if str(group.get('postGroupId') or group.get('_id') or '') != audit['remote_id']:
        return False
    if group.get('platforms') != [runner.policy['platform_id']]:
        return False
    expected_status = 'scheduled' if scheduled else 'draft'
    if group.get('status') != expected_status:
        return False
    if group.get('content') is not None and group['content'] != caption:
        return False
    children = group.get('posts')
    channel = runner.policy['platform_id']
    if (not isinstance(children, list) or len(children) != 1
            or not isinstance(children[0], dict)
            or children[0].get('platform') != 'linkedin'
            or children[0].get('platformId') not in (channel, channel.removeprefix('linkedin-'))
            or children[0].get('content') != caption
            or children[0].get('status') != expected_status
            or children[0].get('postedId') or children[0].get('permalink')):
        return False
    items = group.get('media')
    if not isinstance(items, list) or len(items) != len(media_ids):
        return False
    inventory = {}
    for item in items:
        if not isinstance(item, dict):
            return False
        ident = str(item.get('mediaId') or item.get('_id') or '')
        if (not _identifier(ident) or ident in inventory or item.get('status') != 'ready'
                or item.get('failureReason')):
            return False
        inventory[ident] = item
    if set(inventory) != set(media_ids):
        return False
    replacement_id = audit.get('new_media_id')
    if replacement_id in inventory:
        item = inventory[replacement_id]
        if (item.get('type') != audit['media']['type']
                or item.get('mimeType') != audit['media']['mime_type']):
            return False
    stored_time = group.get('scheduledTime')
    if scheduled or stored_time is not None:
        try:
            actual = datetime.fromisoformat(stored_time.replace('Z', '+00:00'))
            intended = datetime.fromisoformat(audit['scheduled_for'].replace('Z', '+00:00'))
        except (ValueError, TypeError, AttributeError):
            return False
        if actual.tzinfo is None or actual != intended:
            return False
    return True


def _finish_replacement(runner, action, record, audit, asset):
    audit.update(status='scheduled', media_stage='verify-replacement-schedule',
                 media_stage_status='verified', reason='Existing post replacement verified')
    metadata = dict(audit['media'], media_id=audit['new_media_id'], status='ready')
    action.update(status='scheduled', text=audit['text'], media=metadata,
                  format=metadata['format'], asset_sha256=metadata['sha256'],
                  asset_title=metadata['title'], asset_pages=metadata['page_count'],
                  media_scheduled_for=audit['scheduled_for'])
    if isinstance(asset.get('id'), str):
        action['asset_id'] = asset['id']
    if isinstance(asset.get('spec'), dict):
        action['asset_spec'] = asset['spec']
    record.update(text=audit['text'], format=metadata['format'],
                  asset_sha256=metadata['sha256'], media_status='scheduled')
    _checkpoint(runner)
    return audit


def replace_media_post(runner, post_key, asset, *, caption=None):
    """Attach or replace media on an existing post without reserving quota.

    The old ready asset remains attached until the new asset is verified ready.
    Each write has a durable stage checkpoint. Reinvoking an unfinished attempt
    performs read-only reconciliation; it never repeats an ambiguous write.
    """
    if getattr(runner, 'live', True) is not True:
        raise ValueError('Existing media replacement requires an explicit live runner')
    data, metadata = _asset(asset)
    action = runner.state.get('actions', {}).get(post_key)
    if not isinstance(action, dict) or action.get('kind') != 'post':
        raise ValueError('An existing post action is required')
    remote_id = action.get('remote_id')
    if not _identifier(remote_id):
        raise ValueError('An existing post identifier is required')
    records = [item for item in runner.state.get('posts', [])
               if isinstance(item, dict) and item.get('id') == remote_id]
    if len(records) != 1:
        raise ValueError('A unique existing post record is required')
    record = records[0]
    old_caption = require_plain_text(action.get('text'))
    new_caption = require_plain_text(caption if caption is not None else action.get('text'))
    if not old_caption.strip() or not new_caption.strip() or len(new_caption) > 3000:
        raise ValueError('A readable LinkedIn caption is required')
    schedule_text = action.get('media_scheduled_for') or action.get('scheduled_for')
    try:
        schedule = datetime.fromisoformat(schedule_text.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        raise ValueError('The existing post publication time is required') from None
    if schedule.tzinfo is None or runner.now.tzinfo is None:
        raise ValueError('Media replacement requires an aware publication clock')
    schedule_text = schedule.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    audits = action.setdefault('media_replacements', [])
    if not isinstance(audits, list):
        raise ValueError('Existing media replacement history is invalid')
    if audits:
        previous = audits[-1]
        if not isinstance(previous, dict):
            raise ValueError('Existing media replacement history is invalid')
        if previous.get('status') != 'scheduled':
            # A crash can occur after any write, including the final schedule.
            # GET can establish final success, but no stage is silently replayed.
            previous['media_stage'] = 'reconcile-replacement'
            observed = _api(runner, previous, 'get', '/get-post/' + remote_id)
            if observed is None:
                return previous
            group = _group(observed)
            previous['observed_status'] = (group.get('status') if group.get('status')
                                           in ('draft', 'scheduled', 'published', 'failed') else 'unknown')
            previous['observed_media_ids'] = [
                str(item.get('mediaId') or item.get('_id') or '')
                for item in group.get('media', []) if isinstance(item, dict)
                and _identifier(item.get('mediaId') or item.get('_id'))]
            if (previous.get('new_media_id') and previous.get('media', {}).get('sha256') == metadata['sha256']
                    and previous.get('text') == new_caption
                    and _replacement_group(observed, runner, previous,
                                           media_ids=[previous['new_media_id']],
                                           caption=previous['text'], scheduled=True)):
                return _finish_replacement(runner, action, record, previous, asset)
            previous.update(status='media-held', media_stage_status='held',
                            reason='Existing replacement needs reconciliation before any further write')
            _checkpoint(runner)
            return previous
        if previous.get('media', {}).get('sha256') == metadata['sha256'] and previous.get('text') == new_caption:
            return previous
    if schedule <= runner.now:
        raise ValueError('Existing media replacement requires a future publication time')
    start = monotonic()
    audit = {'kind': 'post-media-replacement', 'at': runner.now.isoformat(),
             'remote_id': remote_id, 'text': new_caption, 'previous_text': old_caption,
             'scheduled_for': schedule_text, 'media': metadata,
             'media_stage': 'inspect-existing-media', 'media_stage_status': 'prepared',
             'status': 'inflight'}
    seed = remote_id + ':' + metadata['sha256'] + ':' + new_caption + ':' + audit['at']
    audit['media_operation_id'] = hashlib.sha256(seed.encode()).hexdigest()
    audits.append(audit)
    _checkpoint(runner)
    observed = _api(runner, audit, 'get', '/get-post/' + remote_id)
    if observed is None:
        return audit
    group = _group(observed)
    old_media = group.get('media')
    if (not isinstance(old_media, list) or len(old_media) > 1
            or (old_media and (not isinstance(old_media[0], dict)
                or not _identifier(old_media[0].get('mediaId') or old_media[0].get('_id'))))):
        _hold(runner, audit, 'Existing media inventory is not empty or a single verifiable asset')
        return audit
    old_id = str(old_media[0].get('mediaId') or old_media[0]['_id']) if old_media else None
    old_ids = [old_id] if old_id else []
    audit['old_media_id'] = old_id
    initially_scheduled = group.get('status') == 'scheduled'
    if not _replacement_group(observed, runner, audit, media_ids=old_ids,
                              caption=old_caption, scheduled=initially_scheduled):
        _hold(runner, audit, 'Existing caption, channel, time or ready media could not be verified')
        return audit
    action['status'] = 'media-held'
    record['media_status'] = 'replacement-in-progress'
    _checkpoint(runner)
    if initially_scheduled or old_caption != new_caption:
        _stage(runner, audit, 'pause-existing-post')
        payload = {'status': 'draft'}
        if old_caption != new_caption:
            payload['content'] = new_caption
        paused = _api(runner, audit, 'put', '/update-post/' + remote_id, payload=payload)
        if paused is None:
            return audit
        audit['media_stage_status'] = 'acknowledged'
        _checkpoint(runner)
        audit['media_stage'] = 'verify-existing-draft'
        observed = _api(runner, audit, 'get', '/get-post/' + remote_id)
        if observed is None:
            return audit
        if not _replacement_group(observed, runner, audit, media_ids=old_ids, caption=new_caption):
            _hold(runner, audit, 'Existing post did not become the intended draft')
            return audit
    _stage(runner, audit, 'reserve-replacement-upload')
    reserved = _api(runner, audit, 'post', '/get-upload-url', payload={
        'fileName': metadata['file_name'], 'contentType': metadata['mime_type'],
        'type': metadata['type'], 'postGroupId': remote_id})
    if reserved is None:
        return audit
    new_id = reserved.get('mediaId')
    if not _identifier(new_id) or new_id == old_id:
        _hold(runner, audit, 'Replacement reservation identifier could not be verified', uncertain=True)
    audit.update(new_media_id=new_id, media_stage_status='acknowledged')
    _checkpoint(runner)
    upload_url = reserved.get('uploadUrl')
    if not valid_upload_url(upload_url):
        _hold(runner, audit, 'Replacement upload destination is not approved storage')
        return audit
    _stage(runner, audit, 'upload-replacement-bytes')
    try:
        uploaded = requests.put(upload_url, headers={'Content-Type': metadata['mime_type']},
                                data=data, timeout=90, allow_redirects=False)
    except Exception:
        _hold(runner, audit, 'Replacement storage acknowledgement unavailable', uncertain=True)
    if not 200 <= uploaded.status_code < 300:
        _hold(runner, audit, 'Replacement storage upload was not acknowledged',
              uncertain=not (400 <= uploaded.status_code < 500 and uploaded.status_code not in (408, 429)),
              status=uploaded.status_code)
        return audit
    audit['media_stage_status'] = 'acknowledged'
    _checkpoint(runner)
    _stage(runner, audit, 'complete-replacement-media')
    completed = _api(runner, audit, 'post', '/complete-media/' + new_id)
    if completed is None:
        return audit
    item = completed.get('mediaFile')
    if (not isinstance(item, dict) or str(item.get('_id') or '') != new_id
            or item.get('status') in ('failed', 'invalid')):
        _hold(runner, audit, 'Replacement completion could not be verified')
        return audit
    audit['media_stage_status'] = 'acknowledged'
    _checkpoint(runner)
    audit['media_stage'] = 'verify-replacement-before-delete'
    observed = _api(runner, audit, 'get', '/get-post/' + remote_id)
    if observed is None:
        return audit
    if not _replacement_group(observed, runner, audit, media_ids=old_ids + [new_id], caption=new_caption):
        _hold(runner, audit, 'Both ready assets were not verified; existing media retained')
        return audit
    if schedule <= runner.now + timedelta(seconds=max(0, monotonic() - start)):
        _hold(runner, audit, 'Publication time elapsed before old media removal')
        return audit
    if old_id is not None:
        _stage(runner, audit, 'delete-previous-media')
        removed = _api(runner, audit, 'delete', '/media/' + old_id)
        if removed is None:
            return audit
        audit['media_stage_status'] = 'acknowledged'
        _checkpoint(runner)
        audit['media_stage'] = 'verify-replacement-inventory'
        observed = _api(runner, audit, 'get', '/get-post/' + remote_id)
        if observed is None:
            return audit
        if not _replacement_group(observed, runner, audit, media_ids=[new_id], caption=new_caption):
            _hold(runner, audit, 'Replacement inventory could not be verified after removal')
            return audit
    if schedule <= runner.now + timedelta(seconds=max(0, monotonic() - start)):
        _hold(runner, audit, 'Publication time elapsed before replacement scheduling')
        return audit
    _stage(runner, audit, 'schedule-existing-replacement')
    scheduled = _api(runner, audit, 'put', '/update-post/' + remote_id,
                     payload={'status': 'scheduled', 'scheduledTime': schedule_text})
    if scheduled is None:
        return audit
    audit['media_stage_status'] = 'acknowledged'
    _checkpoint(runner)
    audit['media_stage'] = 'verify-replacement-schedule'
    observed = _api(runner, audit, 'get', '/get-post/' + remote_id)
    if observed is None:
        return audit
    if not _replacement_group(observed, runner, audit, media_ids=[new_id],
                              caption=new_caption, scheduled=True):
        _hold(runner, audit, 'Replacement schedule, caption, time or channel could not be verified')
        return audit
    return _finish_replacement(runner, action, record, audit, asset)
