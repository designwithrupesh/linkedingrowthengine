import copy
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from automation.media_posts import (
    MediaPostCheckpointError, MediaPostOutcomeError, publish_media_post,
    replace_media_post, valid_upload_url, validate_media_asset,
)


GROUP_ID = 'group123'
MEDIA_ID = 'media123'
UPLOAD_URL = 'https://brandcraft-media.s3.amazonaws.com/documents/design.pdf?X-Amz-Signature=never-persist-this'
CHANNEL = 'linkedin-test'
CAPTION = 'A worksheet for choosing the next product problem to solve.'
SCHEDULE = '2026-10-13T03:30:00Z'


def response(payload=None, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


class FakeRunner:
    def __init__(self, action):
        self.policy = {'platform_id': CHANNEL}
        self.now = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
        self.state = {'actions': {'post:test': action}, 'posts': [],
                      'days': {'2026-10-13': {'post': 1}}}
        self.snapshots = []
        self.fail_stage = None

    def save(self):
        if self.fail_stage == self.state['actions']['post:test'].get('media_stage'):
            raise RuntimeError('checkpoint failure with secret in exception')
        self.snapshots.append(copy.deepcopy(self.state))


class MediaPostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'resource.pdf'
        self.path.write_bytes(b'%PDF-1.4\nA locally rendered test document\n%%EOF')
        self.asset = {'path': str(self.path), 'preview_path': 'unused.png',
                      'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(),
                      'mime_type': 'application/pdf', 'page_count': 3,
                      'title': 'Pick the next product problem', 'kind': 'freebie'}
        self.action = {'kind': 'post', 'text': CAPTION, 'status': 'inflight',
                       'at': '2026-10-12T08:00:00+00:00',
                       'scheduled_for': SCHEDULE, 'allocation_day': '2026-10-13'}
        self.runner = FakeRunner(self.action)
        self.enterContext(patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-api-secret'}))
        self.post = self.enterContext(patch('automation.media_posts.requests.post'))
        self.put = self.enterContext(patch('automation.media_posts.requests.put'))
        self.get = self.enterContext(patch('automation.media_posts.requests.get'))
        self.post.side_effect = [
            response({'success': True, 'postGroupId': GROUP_ID}),
            response({'success': True, 'mediaId': MEDIA_ID, 'uploadUrl': UPLOAD_URL,
                      'fileUrl': 'https://media.publora.com/documents/design.pdf'}),
            response({'success': True, 'mediaFile': {'_id': MEDIA_ID, 'status': 'ready'}}),
        ]
        self.put.side_effect = [response(status=200), response({'success': True})]
        self.get.side_effect = [response(self.group()), response(self.group(scheduled=True))]

    def group(self, scheduled=False):
        return {'success': True, 'postGroupId': GROUP_ID,
                'status': 'scheduled' if scheduled else 'draft',
                'scheduledTime': SCHEDULE if scheduled else None,
                'platforms': [CHANNEL],
                'posts': [{'platform': 'linkedin', 'platformId': '123', 'content': CAPTION}],
                'media': [{'mediaId': MEDIA_ID, 'type': 'document',
                           'mimeType': 'application/pdf', 'status': 'ready',
                           'failureReason': None}]}

    def run_post(self):
        return publish_media_post(self.runner, self.action, self.asset)

    def test_pdf_is_drafted_uploaded_validated_then_scheduled_once(self):
        result = self.run_post()
        self.assertEqual(result['status'], 'scheduled')
        self.assertEqual(self.post.call_count, 3)
        self.assertEqual(self.put.call_count, 2)
        self.assertEqual(self.get.call_count, 2)
        self.assertNotIn('scheduledTime', self.post.call_args_list[0].kwargs['json'])
        self.assertEqual(self.post.call_args_list[1].kwargs['json'], {
            'fileName': 'pick-the-next-product-problem.pdf', 'contentType': 'application/pdf',
            'type': 'document', 'postGroupId': GROUP_ID})
        self.assertNotIn('json', self.post.call_args_list[2].kwargs)
        self.assertEqual(self.put.call_args_list[1].kwargs['json'], {
            'status': 'scheduled', 'scheduledTime': SCHEDULE})
        self.assertEqual(self.runner.state['days']['2026-10-13']['post'], 1)
        self.assertEqual(self.runner.state['posts'][0]['media_status'], 'scheduled')

    def test_upload_has_no_api_auth_or_redirect_and_no_signed_url_checkpoint(self):
        self.run_post()
        upload = self.put.call_args_list[0]
        self.assertEqual(upload.args[0], UPLOAD_URL)
        self.assertEqual(upload.kwargs['headers'], {'Content-Type': 'application/pdf'})
        self.assertFalse(upload.kwargs['allow_redirects'])
        for call in self.post.call_args_list + self.get.call_args_list + self.put.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])
        serialized = repr(self.runner.snapshots)
        self.assertNotIn('never-persist-this', serialized)
        self.assertNotIn('test-api-secret', serialized)
        self.assertNotIn('uploadUrl', serialized)
        self.assertNotIn('X-Amz', serialized)

    def test_draft_and_schedule_have_stable_distinct_idempotency_keys(self):
        self.run_post()
        create = self.post.call_args_list[0].kwargs['headers']['Idempotency-Key']
        schedule = self.put.call_args_list[1].kwargs['headers']['Idempotency-Key']
        self.assertNotEqual(create, schedule)
        self.assertEqual(len(create), 64)
        self.assertNotIn('Idempotency-Key', self.post.call_args_list[1].kwargs['headers'])

    def test_acknowledged_draft_checkpoint_precedes_upload_reservation(self):
        self.run_post()
        acknowledged = next(state for state in self.runner.snapshots
                            if state['actions']['post:test'].get('remote_id'))
        self.assertEqual(acknowledged['posts'][0]['id'], GROUP_ID)
        self.assertEqual(acknowledged['actions']['post:test']['media_stage'], 'create-draft')
        self.assertEqual(acknowledged['posts'][0]['media_status'], 'draft')

    def test_checksum_or_invalid_file_blocks_all_requests(self):
        self.asset['sha256'] = 'incorrect'
        with self.assertRaises(ValueError):
            self.run_post()
        self.post.assert_not_called()
        self.put.assert_not_called()
        self.get.assert_not_called()

    def test_pdf_page_count_and_mime_bounds(self):
        for changed in [{'page_count': 301}, {'page_count': True}, {'mime_type': 'image/svg+xml'},
                        {'kind': 'video'}, {'kind': 'image'}]:
            with self.subTest(changed=changed):
                asset = dict(self.asset, **changed)
                with self.assertRaises(ValueError):
                    publish_media_post(self.runner, self.action, asset)
        self.post.assert_not_called()

    def test_known_create_rejection_does_not_upload_or_schedule(self):
        self.post.side_effect = [response({'error': 'private diagnostic'}, status=400)]
        self.run_post()
        self.assertEqual(self.action['status'], 'rejected')
        self.put.assert_not_called()
        self.get.assert_not_called()
        self.assertNotIn('private diagnostic', repr(self.runner.state))

    def test_uncertain_create_is_quarantined_and_second_call_never_recreates(self):
        self.post.side_effect = [response(status=503)]
        with self.assertRaises(MediaPostOutcomeError):
            self.run_post()
        self.assertEqual(self.action['status'], 'unknown-needs-reconciliation')
        self.run_post()
        self.assertEqual(self.post.call_count, 1)
        self.put.assert_not_called()
        self.assertFalse(self.runner.state['posts'])

    def test_missing_draft_identifier_is_unknown_and_not_retried(self):
        self.post.side_effect = [response({'success': True})]
        with self.assertRaises(MediaPostOutcomeError):
            self.run_post()
        self.run_post()
        self.assertEqual(self.post.call_count, 1)
        self.put.assert_not_called()

    def test_upload_reservation_unknown_retains_draft_and_holds(self):
        self.post.side_effect = [response({'success': True, 'postGroupId': GROUP_ID}),
                                 requests.Timeout('signed key would be exposed')]
        with self.assertRaises(MediaPostOutcomeError) as error:
            self.run_post()
        self.assertEqual(self.action['remote_id'], GROUP_ID)
        self.assertEqual(len(self.runner.state['posts']), 1)
        self.assertNotIn('signed key', str(error.exception))
        self.put.assert_not_called()

    def test_unapproved_upload_host_holds_without_sending_bytes(self):
        self.post.side_effect = [response({'success': True, 'postGroupId': GROUP_ID}),
                                 response({'success': True, 'mediaId': MEDIA_ID,
                                           'uploadUrl': 'https://attacker.example/upload?secret=x'})]
        self.run_post()
        self.assertEqual(self.action['status'], 'media-held')
        self.assertEqual(self.action['media']['media_id'], MEDIA_ID)
        self.put.assert_not_called()
        self.get.assert_not_called()
        self.assertNotIn('attacker.example', repr(self.runner.state))

    def test_signed_put_timeout_holds_without_complete_or_schedule(self):
        self.put.side_effect = requests.Timeout('secret-signed-url')
        with self.assertRaises(MediaPostOutcomeError) as error:
            self.run_post()
        self.assertNotIn('secret-signed-url', str(error.exception))
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.put.call_count, 1)
        self.get.assert_not_called()

    def test_signed_put_definitive_rejection_holds_without_retry(self):
        self.put.side_effect = [response(status=403)]
        self.run_post()
        self.assertEqual(self.action['status'], 'media-held')
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.put.call_count, 1)
        self.run_post()
        self.assertEqual(self.put.call_count, 1)

    def test_complete_with_uploaded_state_requires_later_ready_inventory(self):
        self.post.side_effect = [response({'success': True, 'postGroupId': GROUP_ID}),
                                 response({'success': True, 'mediaId': MEDIA_ID, 'uploadUrl': UPLOAD_URL}),
                                 response({'success': True, 'mediaFile': {'_id': MEDIA_ID, 'status': 'uploaded'}})]
        self.run_post()
        self.assertEqual(self.action['status'], 'scheduled')

    def test_bad_completion_id_prevents_schedule(self):
        self.post.side_effect = [response({'success': True, 'postGroupId': GROUP_ID}),
                                 response({'success': True, 'mediaId': MEDIA_ID, 'uploadUrl': UPLOAD_URL}),
                                 response({'success': True, 'mediaFile': {'_id': 'wrong', 'status': 'ready'}})]
        self.run_post()
        self.assertEqual(self.action['status'], 'media-held')
        self.assertEqual(self.put.call_count, 1)
        self.get.assert_not_called()

    def test_pending_or_wrong_media_or_channel_never_schedules(self):
        variants = []
        pending = self.group(); pending['media'][0]['status'] = 'uploading'; variants.append(pending)
        wrong_mime = self.group(); wrong_mime['media'][0]['mimeType'] = 'image/png'; variants.append(wrong_mime)
        wrong_channel = self.group(); wrong_channel['platforms'] = ['linkedin-another']; variants.append(wrong_channel)
        extra_media = self.group(); extra_media['media'].append(dict(extra_media['media'][0])); variants.append(extra_media)
        changed_caption = self.group(); changed_caption['posts'][0]['content'] = 'Something else'; variants.append(changed_caption)
        for group in variants:
            with self.subTest(group=group):
                self.setUp()
                self.get.side_effect = [response(group)]
                self.run_post()
                self.assertEqual(self.action['status'], 'media-held')
                self.assertEqual(self.put.call_count, 1)

    def test_schedule_http_failure_keeps_media_receipt_and_never_creates_again(self):
        self.put.side_effect = [response(), response(status=503)]
        with self.assertRaises(MediaPostOutcomeError):
            self.run_post()
        self.assertEqual(self.action['media']['status'], 'ready')
        self.run_post()
        self.assertEqual(self.post.call_count, 3)
        self.assertEqual(self.put.call_count, 2)

    def test_elapsed_publication_time_holds_draft_before_schedule_intent(self):
        self.action['scheduled_for'] = '2026-10-12T08:05:00Z'
        self.action['allocation_day'] = '2026-10-12'
        with patch('automation.media_posts.monotonic', side_effect=[100.0, 401.0]):
            self.run_post()
        self.assertEqual(self.action['status'], 'media-held')
        self.assertEqual(self.action['media']['status'], 'ready')
        self.assertEqual(self.put.call_count, 1)  # Storage upload only.
        self.assertEqual(self.get.call_count, 1)
        self.assertFalse(any(snapshot['actions']['post:test']['media_stage'] == 'schedule-media'
                             for snapshot in self.runner.snapshots))
        self.assertEqual(self.runner.state['posts'][0]['media_status'], 'ready')

    def test_schedule_at_elapsed_deadline_is_held_without_time_coercion(self):
        self.action['scheduled_for'] = '2026-10-12T08:05:00Z'
        self.action['allocation_day'] = '2026-10-12'
        with patch('automation.media_posts.monotonic', side_effect=[100.0, 400.0]):
            self.run_post()
        self.assertEqual(self.action['status'], 'media-held')
        self.assertEqual(self.put.call_count, 1)

    def test_final_verification_wrong_time_or_status_holds(self):
        group = self.group(scheduled=True)
        group['scheduledTime'] = '2026-10-13T04:30:00Z'
        self.get.side_effect = [response(self.group()), response(group)]
        self.run_post()
        self.assertEqual(self.action['status'], 'media-held')
        self.assertEqual(self.runner.state['posts'][0]['media_status'], 'schedule-acknowledged')

    def test_failed_checkpoint_prevents_next_remote_step(self):
        self.runner.fail_stage = 'reserve-upload'
        with self.assertRaises(MediaPostCheckpointError):
            self.run_post()
        self.assertEqual(self.post.call_count, 1)
        self.put.assert_not_called()
        self.assertEqual(self.action['remote_id'], GROUP_ID)

    def test_initial_checkpoint_failure_sends_nothing(self):
        self.runner.fail_stage = 'create-draft'
        with self.assertRaises(MediaPostCheckpointError):
            self.run_post()
        self.post.assert_not_called()

    def test_png_is_uploaded_as_image_instead_of_document(self):
        from PIL import Image
        buffer = io.BytesIO()
        Image.new('RGB', (1200, 1500), 'white').save(buffer, format='PNG')
        data = buffer.getvalue()
        self.path.write_bytes(data)
        self.asset.update(mime_type='image/png', sha256=hashlib.sha256(data).hexdigest(), kind='image', page_count=None)
        draft = self.group(); final = self.group(scheduled=True)
        for group in (draft, final):
            group['media'][0].update(type='image', mimeType='image/png')
        self.get.side_effect = [response(draft), response(final)]
        self.run_post()
        self.assertEqual(self.action['status'], 'scheduled')
        reserve = self.post.call_args_list[1].kwargs['json']
        self.assertEqual(reserve['type'], 'image')
        self.assertEqual(reserve['contentType'], 'image/png')
        self.assertTrue(reserve['fileName'].endswith('.png'))

    def test_public_asset_validation_has_no_writes_or_quota_changes(self):
        metadata = validate_media_asset(self.asset)
        self.assertEqual(metadata['type'], 'document')
        self.assertEqual(metadata['sha256'], self.asset['sha256'])
        self.assertEqual(self.runner.state['days']['2026-10-13']['post'], 1)
        self.assertFalse(self.runner.snapshots)
        self.post.assert_not_called()

    def test_truncated_png_with_recomputed_hash_is_rejected(self):
        self.path.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00\x00\x00\x0dIHDR' + b'\x00\x00\x00\x01' * 2)
        self.asset.update(mime_type='image/png', kind='image', page_count=None,
                          sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())
        with self.assertRaises(ValueError):
            validate_media_asset(self.asset)
        self.post.assert_not_called()


class SignedURLTests(unittest.TestCase):
    def test_documented_s3_hosts(self):
        for url in ['https://brandcraft-media.s3.amazonaws.com/a.pdf?X-Amz-Signature=x',
                    'https://bucket.s3.us-east-1.amazonaws.com/a',
                    'https://bucket.s3-us-east-1.amazonaws.com/a',
                    'https://s3.us-east-1.amazonaws.com/bucket/a']:
            self.assertTrue(valid_upload_url(url), url)

    def test_private_spoofed_credentialed_and_redirect_hosts_rejected(self):
        for url in ['http://brandcraft-media.s3.amazonaws.com/a',
                    'https://brandcraft-media.s3.amazonaws.com.evil.test/a',
                    'https://169.254.169.254/latest/meta-data/',
                    'https://127.0.0.1/upload', 'https://localhost/upload',
                    'https://user:password@bucket.s3.amazonaws.com/a',
                    'https://bucket.s3.amazonaws.com:444/a',
                    'https://bucket.s3.amazonaws.com/a#fragment',
                    'https://bucket.s3.amazonaws.com/',
                    'https://bucket.s3.amazonaws.com/a\nmalformed']:
            self.assertFalse(valid_upload_url(url), url)


class ExistingMediaReplacementTests(unittest.TestCase):
    """Replacement must preserve identity and retain old bytes until verified."""

    OLD_MEDIA = 'oldmedia123'

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'redesigned.pdf'
        path.write_bytes(b'%PDF-1.4\nA redesigned document\n%%EOF')
        self.asset = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                      'mime_type': 'application/pdf', 'page_count': 7,
                      'title': 'Product decisions review', 'kind': 'carousel',
                      'id': 'product-decisions', 'spec': {'title': 'Product decisions review'}}
        self.action = {'kind': 'post', 'text': CAPTION, 'status': 'scheduled',
                       'remote_id': GROUP_ID, 'scheduled_for': SCHEDULE,
                       'media_scheduled_for': SCHEDULE, 'allocation_day': '2026-10-13',
                       'media': {'media_id': self.OLD_MEDIA, 'sha256': 'old-sha'}}
        self.runner = FakeRunner(self.action)
        self.runner.live = True
        self.runner.state['posts'] = [{'id': GROUP_ID, 'text': CAPTION,
                                      'scheduled_for': SCHEDULE, 'media_status': 'scheduled'}]
        self.runner.save = self.save
        self.fail_stage = None
        self.sequence = []
        self.enterContext(patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-api-secret'}))
        self.post = self.enterContext(patch('automation.media_posts.requests.post'))
        self.put = self.enterContext(patch('automation.media_posts.requests.put'))
        self.get = self.enterContext(patch('automation.media_posts.requests.get'))
        self.delete = self.enterContext(patch('automation.media_posts.requests.delete'))
        self.post.side_effect = [response({'success': True, 'mediaId': MEDIA_ID,
                                          'uploadUrl': UPLOAD_URL}),
                                 response({'success': True,
                                           'mediaFile': {'_id': MEDIA_ID, 'status': 'ready'}})]
        self.put.side_effect = [response({'success': True}), response(), response({'success': True})]
        self.delete.return_value = response({'success': True})
        self.get.side_effect = [response(self.group('scheduled', old=True)),
                                response(self.group('draft', old=True)),
                                response(self.group('draft', old=True, new=True)),
                                response(self.group('draft', new=True)),
                                response(self.group('scheduled', new=True))]

    def save(self):
        audit = self.action['media_replacements'][-1]
        self.sequence.append(('checkpoint', audit['media_stage']))
        if self.fail_stage == audit['media_stage']:
            raise RuntimeError('private checkpoint diagnostic')
        self.runner.snapshots.append(copy.deepcopy(self.runner.state))

    def group(self, status, *, old=False, new=False, caption=CAPTION):
        media = []
        if old:
            media.append({'mediaId': self.OLD_MEDIA, 'type': 'document',
                          'mimeType': 'application/pdf', 'status': 'ready', 'failureReason': None})
        if new:
            media.append({'mediaId': MEDIA_ID, 'type': 'document', 'mimeType': 'application/pdf',
                          'status': 'ready', 'failureReason': None})
        return {'success': True, 'postGroupId': GROUP_ID, 'status': status,
                'scheduledTime': SCHEDULE, 'platforms': [CHANNEL], 'media': media,
                'posts': [{'platform': 'linkedin', 'platformId': 'test',
                           'status': status, 'content': caption, 'postedId': None, 'permalink': None}]}

    def run_replacement(self, **kwargs):
        return replace_media_post(self.runner, 'post:test', self.asset, **kwargs)

    def test_existing_post_replacement_keeps_id_caption_time_and_quota(self):
        counts = copy.deepcopy(self.runner.state['days'])
        result = self.run_replacement()
        self.assertEqual(result['status'], 'scheduled')
        self.assertEqual(self.action['remote_id'], GROUP_ID)
        self.assertEqual(self.action['text'], CAPTION)
        self.assertEqual(self.action['scheduled_for'], SCHEDULE)
        self.assertEqual(self.action['media']['media_id'], MEDIA_ID)
        self.assertEqual(self.runner.state['posts'][0]['id'], GROUP_ID)
        self.assertEqual(self.runner.state['days'], counts)
        self.assertEqual(len(self.runner.state['actions']), 1)
        self.assertEqual(len(self.runner.state['posts']), 1)
        self.assertEqual(self.post.call_count, 2)
        self.assertEqual(self.delete.call_count, 1)
        self.assertEqual(self.delete.call_args.args[0], 'https://api.publora.com/api/v1/media/' + self.OLD_MEDIA)
        self.assertEqual(self.put.call_args_list[0].kwargs['json'], {'status': 'draft'})
        self.assertEqual(self.put.call_args_list[2].kwargs['json'],
                         {'status': 'scheduled', 'scheduledTime': SCHEDULE})
        self.assertFalse(any('/create-post' in call.args[0] for call in self.post.call_args_list))

    def test_already_paused_draft_needs_no_redundant_pause(self):
        self.get.side_effect = [response(self.group('draft', old=True)),
                                response(self.group('draft', old=True, new=True)),
                                response(self.group('draft', new=True)),
                                response(self.group('scheduled', new=True))]
        self.put.side_effect = [response(), response({'success': True})]
        self.run_replacement()
        self.assertEqual(self.put.call_count, 2)
        self.assertNotIn('json', self.put.call_args_list[0].kwargs)

    def test_old_media_delete_follows_verified_new_ready_inventory(self):
        def delete_after_inventory(*args, **kwargs):
            self.assertEqual(self.get.call_count, 3)
            self.assertEqual(self.post.call_count, 2)
            snapshot = self.runner.snapshots[-1]['actions']['post:test']['media_replacements'][-1]
            self.assertEqual(snapshot['media_stage'], 'delete-previous-media')
            self.assertEqual(snapshot['new_media_id'], MEDIA_ID)
            self.assertEqual(snapshot['old_media_id'], self.OLD_MEDIA)
            return response({'success': True})
        self.delete.side_effect = delete_after_inventory
        self.run_replacement()

    def test_pending_new_media_retains_old_and_never_schedules(self):
        group = self.group('draft', old=True, new=True)
        group['media'][1]['status'] = 'uploading'
        self.get.side_effect = [response(self.group('scheduled', old=True)),
                                response(self.group('draft', old=True)), response(group)]
        result = self.run_replacement()
        self.assertEqual(result['status'], 'media-held')
        self.delete.assert_not_called()
        self.assertEqual(self.put.call_count, 2)  # Pause plus storage upload.
        self.assertEqual(self.action['media']['media_id'], self.OLD_MEDIA)

    def test_mismatched_inventory_or_caption_never_deletes_old_media(self):
        bad_groups = []
        wrong_account = self.group('draft', old=True, new=True)
        wrong_account['platforms'] = ['linkedin-other']; bad_groups.append(wrong_account)
        wrong_caption = self.group('draft', old=True, new=True, caption='Unexpected caption.')
        bad_groups.append(wrong_caption)
        missing_old = self.group('draft', new=True); bad_groups.append(missing_old)
        wrong_mime = self.group('draft', old=True, new=True)
        wrong_mime['media'][1]['mimeType'] = 'image/png'; bad_groups.append(wrong_mime)
        for group in bad_groups:
            with self.subTest(group=group):
                self.setUp()
                self.get.side_effect = [response(self.group('scheduled', old=True)),
                                        response(self.group('draft', old=True)), response(group)]
                self.assertEqual(self.run_replacement()['status'], 'media-held')
                self.delete.assert_not_called()

    def test_changed_existing_group_blocks_all_writes(self):
        original = self.group('scheduled', old=True)
        original['posts'][0]['content'] = 'Changed by someone else.'
        self.get.side_effect = [response(original)]
        self.assertEqual(self.run_replacement()['status'], 'media-held')
        self.post.assert_not_called()
        self.put.assert_not_called()
        self.delete.assert_not_called()

    def test_existing_published_group_is_never_changed(self):
        self.get.side_effect = [response(self.group('published', old=True))]
        self.assertEqual(self.run_replacement()['status'], 'media-held')
        self.post.assert_not_called()
        self.put.assert_not_called()
        self.delete.assert_not_called()

    def test_optional_caption_edit_happens_with_draft_pause_and_is_verified(self):
        caption = 'A practical review of the promise and the first product task.'
        self.get.side_effect = [response(self.group('scheduled', old=True)),
                                response(self.group('draft', old=True, caption=caption)),
                                response(self.group('draft', old=True, new=True, caption=caption)),
                                response(self.group('draft', new=True, caption=caption)),
                                response(self.group('scheduled', new=True, caption=caption))]
        self.run_replacement(caption=caption)
        self.assertEqual(self.put.call_args_list[0].kwargs['json'], {'status': 'draft', 'content': caption})
        self.assertEqual(self.action['text'], caption)
        self.assertEqual(self.runner.state['posts'][0]['text'], caption)

    def test_unapproved_signed_upload_host_retains_old_media_and_never_uploads(self):
        self.post.side_effect = [response({'success': True, 'mediaId': MEDIA_ID,
                                          'uploadUrl': 'https://evil.example/?private=x'})]
        self.assertEqual(self.run_replacement()['status'], 'media-held')
        self.assertEqual(self.put.call_count, 1)  # Pause only.
        self.delete.assert_not_called()
        self.assertNotIn('evil.example', repr(self.runner.snapshots))

    def test_replacement_checkpoints_never_persist_signed_url_or_api_key(self):
        self.run_replacement()
        serialized = repr(self.runner.snapshots)
        for private in ('never-persist-this', 'test-api-secret', 'uploadUrl', 'X-Amz'):
            self.assertNotIn(private, serialized)
        upload = self.put.call_args_list[1]
        self.assertEqual(upload.kwargs['headers'], {'Content-Type': 'application/pdf'})
        for call in self.post.call_args_list + self.get.call_args_list + self.put.call_args_list + self.delete.call_args_list:
            self.assertFalse(call.kwargs['allow_redirects'])

    def test_uncertain_delete_is_not_repeated_and_only_get_reconciles(self):
        self.delete.side_effect = requests.Timeout('secret provider diagnostic')
        with self.assertRaises(MediaPostOutcomeError) as error:
            self.run_replacement()
        self.assertNotIn('secret provider', str(error.exception))
        before = (self.put.call_count, self.post.call_count, self.delete.call_count)
        self.get.side_effect = [response(self.group('draft', new=True))]
        result = self.run_replacement()
        self.assertEqual(result['status'], 'media-held')
        self.assertEqual((self.put.call_count, self.post.call_count, self.delete.call_count), before)
        self.assertEqual(len(self.action['media_replacements']), 1)

    def test_unknown_final_schedule_can_be_verified_by_get_without_replaying_it(self):
        self.put.side_effect = [response({'success': True}), response(), requests.Timeout('unknown schedule')]
        with self.assertRaises(MediaPostOutcomeError):
            self.run_replacement()
        before = (self.put.call_count, self.post.call_count, self.delete.call_count)
        self.get.side_effect = [response(self.group('scheduled', new=True))]
        result = self.run_replacement()
        self.assertEqual(result['status'], 'scheduled')
        self.assertEqual((self.put.call_count, self.post.call_count, self.delete.call_count), before)
        self.assertEqual(self.action['media']['media_id'], MEDIA_ID)

    def test_completed_identical_replacement_is_a_noop(self):
        self.run_replacement()
        counts = (self.get.call_count, self.put.call_count, self.post.call_count, self.delete.call_count)
        self.run_replacement()
        self.assertEqual((self.get.call_count, self.put.call_count, self.post.call_count, self.delete.call_count), counts)
        self.assertEqual(len(self.action['media_replacements']), 1)

    def test_failed_checkpoint_before_old_delete_is_fatal_and_retains_old_asset(self):
        self.fail_stage = 'delete-previous-media'
        with self.assertRaises(MediaPostCheckpointError) as error:
            self.run_replacement()
        self.assertNotIn('private checkpoint', str(error.exception))
        self.delete.assert_not_called()
        self.assertEqual(self.put.call_count, 2)

    def test_failed_checkpoint_before_pause_prevents_every_write(self):
        self.fail_stage = 'pause-existing-post'
        with self.assertRaises(MediaPostCheckpointError):
            self.run_replacement()
        self.post.assert_not_called()
        self.put.assert_not_called()
        self.delete.assert_not_called()

    def test_failed_initial_checkpoint_prevents_even_inventory_read(self):
        self.fail_stage = 'inspect-existing-media'
        with self.assertRaises(MediaPostCheckpointError):
            self.run_replacement()
        self.get.assert_not_called()
        self.post.assert_not_called()

    def test_elapsed_time_before_delete_retains_both_assets_without_schedule(self):
        with patch('automation.media_posts.monotonic', side_effect=[0, 100000]):
            result = self.run_replacement()
        self.assertEqual(result['status'], 'media-held')
        self.delete.assert_not_called()
        self.assertEqual(self.put.call_count, 2)

    def test_elapsed_time_after_delete_never_coerces_scheduling_to_now(self):
        with patch('automation.media_posts.monotonic', side_effect=[0, 0, 100000]):
            result = self.run_replacement()
        self.assertEqual(result['status'], 'media-held')
        self.assertEqual(self.delete.call_count, 1)
        self.assertEqual(self.put.call_count, 2)

    def test_expired_publication_or_dry_run_or_wrong_hash_blocks_all_requests(self):
        for variant in ('expired', 'preview', 'checksum'):
            with self.subTest(variant=variant):
                self.setUp()
                if variant == 'expired':
                    self.action['media_scheduled_for'] = '2026-10-12T07:00:00Z'
                elif variant == 'preview':
                    self.runner.live = False
                else:
                    self.asset['sha256'] = 'wrong'
                with self.assertRaises(ValueError):
                    self.run_replacement()
                self.get.assert_not_called()
                self.post.assert_not_called()
                self.put.assert_not_called()
                self.delete.assert_not_called()

    def test_old_caption_whitespace_is_preserved_exactly(self):
        caption = '  ' + CAPTION + '\n'
        self.action['text'] = caption
        self.runner.state['posts'][0]['text'] = caption
        self.get.side_effect = [response(self.group('scheduled', old=True, caption=caption)),
                                response(self.group('draft', old=True, caption=caption)),
                                response(self.group('draft', old=True, new=True, caption=caption)),
                                response(self.group('draft', new=True, caption=caption)),
                                response(self.group('scheduled', new=True, caption=caption))]
        self.run_replacement()
        self.assertEqual(self.action['text'], caption)


if __name__ == '__main__':
    unittest.main()
