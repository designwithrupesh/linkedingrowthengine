from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import Mock

import requests

from automation.comment_signal import (CommentSignals, FALLBACK_SECONDS,
                                       MAX_STATISTICS_COOLDOWN_SECONDS, RETRY_SECONDS,
                                       STATISTICS_RESOURCE, STATISTICS_URL)


URN = 'urn:li:share:7514165310896250880'
POST = {'post_urn': URN}
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


def session(analytics=True, count=3):
    result = Mock()
    result.get.return_value = response({'success': True, 'context': {
        'features': {'apiAccess': True, 'analytics': analytics}}})
    result.post.return_value = response({'success': True, 'count': count, 'cached': False})
    return result


def state(last_attempt=None, last_success=None):
    record = {}
    if last_attempt is not None:
        record['last_attempt_at'] = last_attempt.isoformat()
    if last_success is not None:
        record['last_success_at'] = last_success.isoformat()
    return {'reply_monitor': {'posts': {URN: record}}}


class CommentSignalTests(unittest.TestCase):
    def test_observed_linkedin_resource_cooldown_blocks_every_post_across_restarts(self):
        observed = datetime(2026, 10, 23, 11, 43, 46, tzinfo=timezone.utc)
        transport, saved = session(), state(observed, observed)
        transport.post.return_value = response({
            'error': 'LINKEDIN_RATE_LIMITED', 'retryAfter': 44174, 'quotaScope': 'application',
            'resource': STATISTICS_RESOURCE,
            'message': 'LinkedIn memberCreatorPostAnalytics:FINDER:entity is cooling down (application).',
            'details': 'private-provider-body',
        }, 429)
        self.assertFalse(CommentSignals('linkedin-owner', 'secret', transport).due(POST, saved, observed))
        self.assertEqual(saved['comment_signals']['statistics_cooldown'], {
            'status': 'rate-limited', 'resource': STATISTICS_RESOURCE, 'scope': 'application',
            'until': '2026-10-24T00:00:00+00:00', 'observed_at': observed.isoformat(),
            'reason': 'linkedin-statistics-rate-limited',
        })
        self.assertNotIn('private-provider-body', str(saved))
        self.assertNotIn('secret', str(saved))
        restarted, persisted = session(), json.loads(json.dumps(saved))
        signals = CommentSignals('linkedin-owner', 'secret', restarted)
        for index in range(5):
            other = {'post_urn': f'urn:li:share:{7514165310896250881 + index}'}
            self.assertTrue(signals.due(other, persisted, observed + timedelta(minutes=5)))
        self.assertFalse(signals.due(POST, persisted, observed + timedelta(hours=1)))
        self.assertTrue(signals.due(POST, persisted, observed + timedelta(seconds=FALLBACK_SECONDS)))
        restarted.get.assert_not_called()
        restarted.post.assert_not_called()
        self.assertNotIn('publishing', persisted)
        self.assertNotIn('actions', persisted)

    def test_resource_expiry_rechecks_even_after_a_recent_fallback_poll(self):
        transport, saved = session(), state(NOW, NOW)
        transport.post.return_value = response({'error': 'LINKEDIN_RATE_LIMITED', 'retryAfter': 44174}, 429)
        self.assertFalse(CommentSignals('linkedin-owner', 'secret', transport).due(POST, saved, NOW))
        expires = NOW + timedelta(seconds=44174)
        before = session()
        signal = CommentSignals('linkedin-owner', 'secret', before)
        self.assertTrue(signal.due(POST, saved, expires - timedelta(seconds=1)))
        before.post.assert_not_called()
        self.assertTrue(signal.due(POST, saved, expires))
        before.post.assert_called_once()
        self.assertEqual(saved['comment_signals']['posts'][URN]['status'], 'analytics-count')

    def test_invalid_retry_values_receive_a_conservative_bounded_resource_backoff(self):
        for value in (None, 0, -1, True, '44174', float('nan'), float('inf'), [], {}):
            with self.subTest(value=value):
                transport, saved = session(), state()
                transport.post.return_value = response({'error': 'LINKEDIN_RATE_LIMITED',
                    'retryAfter': value, 'quotaScope': 'private-provider-body'}, 429)
                CommentSignals('linkedin-owner', 'secret', transport).due(POST, saved, NOW)
                cooldown = saved['comment_signals']['statistics_cooldown']
                self.assertEqual(cooldown['until'], (NOW + timedelta(seconds=RETRY_SECONDS)).isoformat())
                self.assertEqual(cooldown['scope'], 'unknown')
                self.assertNotIn('private-provider-body', str(saved))
        for value, seconds in ((0.5, 1), (10 ** 200, MAX_STATISTICS_COOLDOWN_SECONDS)):
            transport, saved = session(), state()
            transport.post.return_value = response({'retryAfter': value}, 429)
            CommentSignals('linkedin-owner', 'secret', transport).due(POST, saved, NOW)
            self.assertEqual(saved['comment_signals']['statistics_cooldown']['until'],
                             (NOW + timedelta(seconds=seconds)).isoformat())

    def test_retry_after_header_can_supply_seconds_or_http_date_without_storing_headers(self):
        for header, seconds in (('1800', 1800), ('Fri, 09 Oct 2026 11:00:00 GMT', 3600),
                                ('-1', RETRY_SECONDS), ('00000000000000000000', RETRY_SECONDS),
                                ('private-provider-body', RETRY_SECONDS)):
            with self.subTest(header=header):
                transport, saved = session(), state()
                limited = response({}, 429)
                limited.headers = {'Retry-After': header, 'x-private-token': 'secret'}
                transport.post.return_value = limited
                CommentSignals('linkedin-owner', 'secret', transport).due(POST, saved, NOW)
                self.assertEqual(saved['comment_signals']['statistics_cooldown']['until'],
                                 (NOW + timedelta(seconds=seconds)).isoformat())
                self.assertNotIn('secret', str(saved))
                self.assertNotIn('private-provider-body', str(saved))

    def test_longer_of_valid_json_and_header_retry_instructions_wins(self):
        transport, saved = session(), state()
        limited = response({'retryAfter': 1800}, 429)
        limited.headers = {'Retry-After': '3600'}
        transport.post.return_value = limited
        CommentSignals('linkedin-owner', 'secret', transport).due(POST, saved, NOW)
        self.assertEqual(saved['comment_signals']['statistics_cooldown']['until'],
                         (NOW + timedelta(hours=1)).isoformat())

    def test_invalid_429_json_still_backs_off_but_other_errors_do_not_create_a_resource_limit(self):
        transport, saved = session(), state()
        limited = response({}, 429)
        limited.json.side_effect = ValueError('private-provider-body')
        transport.post.return_value = limited
        CommentSignals('linkedin-owner', 'secret', transport).due(POST, saved, NOW)
        self.assertEqual(saved['comment_signals']['statistics_cooldown']['reason'], 'statistics-rate-limited')
        self.assertNotIn('private-provider-body', str(saved))
        for status in (403, 500):
            transport, saved = session(), state()
            transport.post.return_value = response({'retryAfter': 44174}, status)
            CommentSignals('linkedin-owner', 'secret', transport).due(POST, saved, NOW)
            self.assertNotIn('statistics_cooldown', saved['comment_signals'])

    def test_rate_limited_selection_keeps_fair_rotation_and_pending_pagination(self):
        last_read = NOW - timedelta(seconds=FALLBACK_SECONDS)
        transport, saved = session(), state(last_read, last_read)
        transport.post.return_value = response({'retryAfter': 44174}, 429)
        signals = CommentSignals('linkedin-owner', 'secret', transport)
        signals.due(POST, saved, NOW)
        posts = [{'post_urn': f'urn:li:share:{7514165310896250880 + index}'} for index in range(12)]
        observed = []
        for cycle in range(3):
            now = NOW + timedelta(minutes=5 * cycle)
            chosen = signals.select(posts, saved, now)
            observed.extend(item['post_urn'] for item in chosen)
            for item in chosen:
                signals.due(item, saved, now)
        self.assertEqual(set(observed), {item['post_urn'] for item in posts})
        transport.post.assert_called_once()
        saved['reply_monitor']['posts'][URN]['next_page'] = 2
        self.assertEqual(signals.select([POST], saved, NOW + timedelta(minutes=20)), [POST])

    def test_first_count_then_success_and_unchanged_skip(self):
        transport, saved = session(), state()
        signals = CommentSignals('linkedin-owner', 'secret', transport)
        self.assertTrue(signals.due(POST, saved, NOW))
        saved['reply_monitor']['posts'][URN].update(last_attempt_at=NOW.isoformat(), last_success_at=NOW.isoformat())
        self.assertFalse(signals.due(POST, saved, NOW + timedelta(minutes=5)))
        transport.get.assert_called_once()
        transport.post.assert_called_with(STATISTICS_URL,
            headers={'x-publora-key': 'secret', 'User-Agent': 'Mozilla/5.0'},
            json={'postedId': URN, 'platformId': 'linkedin-owner', 'queryType': 'COMMENT'},
            timeout=20, allow_redirects=False)

    def test_changed_count_is_due_and_failed_fetch_retries_after_backoff(self):
        transport, saved = session(), state()
        signals = CommentSignals('linkedin-owner', 'secret', transport)
        self.assertTrue(signals.due(POST, saved, NOW))
        saved['reply_monitor']['posts'][URN].update(last_attempt_at=NOW.isoformat(), last_success_at=NOW.isoformat())
        transport.post.return_value = response({'success': True, 'count': 4, 'cached': True})
        changed = NOW + timedelta(hours=2)
        self.assertTrue(signals.due(POST, saved, changed))
        saved['reply_monitor']['posts'][URN]['last_attempt_at'] = changed.isoformat()
        self.assertFalse(signals.due(POST, saved, changed + timedelta(minutes=5)))
        self.assertTrue(signals.due(POST, saved, changed + timedelta(minutes=15)))

    def test_statistics_cooldown_survives_a_new_runner_instance(self):
        transport, saved = session(), state()
        self.assertTrue(CommentSignals('linkedin-owner', 'secret', transport).due(POST, saved, NOW))
        saved['reply_monitor']['posts'][URN].update(last_attempt_at=NOW.isoformat(), last_success_at=NOW.isoformat())
        restarted = session(count=4)
        signal = CommentSignals('linkedin-owner', 'secret', restarted)
        self.assertFalse(signal.due(POST, saved, NOW + timedelta(minutes=5)))
        restarted.get.assert_not_called()
        restarted.post.assert_not_called()
        self.assertTrue(signal.due(POST, saved, NOW + timedelta(hours=2)))
        restarted.post.assert_called_once()

    def test_sixty_posts_rotate_in_batches_of_five_without_network_during_selection(self):
        posts = [{'post_urn': f'urn:li:share:{7514165310896250880 + index}'} for index in range(60)]
        transport, saved = session(), {}
        signal = CommentSignals('linkedin-owner', 'secret', transport)
        observed = []
        for index in range(12):
            chosen = signal.select(posts, saved, NOW + timedelta(minutes=5 * index), limit=60)
            self.assertEqual(len(chosen), 5)
            observed.extend(post['post_urn'] for post in chosen)
            # The next instance must honor the same durable rotation.
            signal = CommentSignals('linkedin-owner', 'secret', transport)
        self.assertEqual(len(set(observed)), 60)
        transport.get.assert_not_called()
        transport.post.assert_not_called()

    def test_cached_unchanged_count_does_not_hide_pending_pagination(self):
        saved = state(NOW, NOW)
        saved['reply_monitor']['posts'][URN]['next_page'] = 2
        saved['comment_signals'] = {'posts': {URN: {'count': 3, 'status': 'analytics-count',
            'changed_at': NOW.isoformat(), 'last_checked_at': NOW.isoformat()}}}
        transport = session()
        signal = CommentSignals('linkedin-owner', 'secret', transport)
        self.assertEqual(signal.select([POST], saved, NOW + timedelta(minutes=5)), [POST])
        transport.get.assert_not_called()
        transport.post.assert_not_called()

    def test_count_decrease_also_requests_reconciliation(self):
        transport, saved = session(), state(NOW, NOW)
        saved['comment_signals'] = {'posts': {URN: {'count': 4, 'changed_at': NOW.isoformat()}}}
        self.assertTrue(CommentSignals('linkedin-owner', 'secret', transport).due(POST, saved, NOW + timedelta(hours=1)))

    def test_stable_count_reconciles_after_24_hours(self):
        transport, saved = session(), state(NOW, NOW)
        saved['comment_signals'] = {'posts': {URN: {'count': 3, 'changed_at': NOW.isoformat()}}}
        signals = CommentSignals('linkedin-owner', 'secret', transport)
        self.assertFalse(signals.due(POST, saved, NOW + timedelta(hours=23)))
        self.assertTrue(signals.due(POST, saved, NOW + timedelta(hours=24)))

    def test_starter_has_no_statistics_call_and_six_hour_fallback(self):
        transport, saved = session(False), state(NOW, NOW)
        signals = CommentSignals('linkedin-owner', 'secret', transport)
        self.assertFalse(signals.due(POST, saved, NOW + timedelta(seconds=FALLBACK_SECONDS - 1)))
        self.assertTrue(signals.due(POST, saved, NOW + timedelta(seconds=FALLBACK_SECONDS)))
        transport.post.assert_not_called()
        transport.get.assert_called_once()

    def test_missing_feature_unknown_context_and_redirect_use_fallback(self):
        for payload, status in [({'success': True, 'context': {'features': {'apiAccess': True}}}, 200),
                                ({'success': False, 'context': {'features': {'apiAccess': True, 'analytics': True}}}, 200),
                                ({'success': True, 'context': {'features': {'analytics': True}}}, 200),
                                ({'secret': 'provider response'}, 302),
                                ([], 200)]:
            with self.subTest(payload=payload, status=status):
                transport, saved = session(), state(NOW, NOW)
                transport.get.return_value = response(payload, status)
                signals = CommentSignals('linkedin-owner', 'secret', transport)
                self.assertFalse(signals.due(POST, saved, NOW + timedelta(minutes=5)))
                transport.post.assert_not_called()
                self.assertNotIn('provider response', str(saved))

    def test_unknown_count_uses_fallback_and_never_stores_provider_body(self):
        for payload in [{'success': True, 'count': None}, {'success': True, 'count': -1},
                        {'success': True, 'count': True}, {'success': True, 'count': '3'},
                        {'success': False, 'count': 3, 'error': 'secret'},
                        {'success': True, 'metrics': {'COMMENT': 3}}, []]:
            with self.subTest(payload=payload):
                transport, saved = session(), state(NOW, NOW)
                transport.post.return_value = response(payload)
                signals = CommentSignals('linkedin-owner', 'secret', transport)
                self.assertFalse(signals.due(POST, saved, NOW + timedelta(minutes=5)))
                self.assertEqual(saved['comment_signals']['posts'][URN]['status'], 'fallback-unverified-count')
                self.assertNotIn('secret', str(saved))

    def test_request_failures_are_sanitized_and_context_checked_once(self):
        transport, saved = session(), state(NOW, NOW)
        transport.get.side_effect = requests.ConnectionError('secret token provider body')
        signals = CommentSignals('linkedin-owner', 'secret', transport)
        self.assertFalse(signals.due(POST, saved, NOW + timedelta(minutes=5)))
        self.assertTrue(signals.due(POST, saved, NOW + timedelta(hours=6)))
        self.assertNotIn('secret', str(saved))
        transport.get.assert_called_once()

    def test_missing_token_permits_bounded_fallback_without_network(self):
        transport, saved = session(), state(NOW, NOW)
        self.assertFalse(CommentSignals('linkedin-owner', None, transport).due(POST, saved, NOW + timedelta(minutes=5)))
        transport.get.assert_not_called()
        transport.post.assert_not_called()

    def test_invalid_identity_and_naive_clock_fail_before_network(self):
        transport = session()
        with self.assertRaises(ValueError):
            CommentSignals('bad connection', 'secret', transport)
        signals = CommentSignals('linkedin-owner', 'secret', transport)
        for post, now in [({'post_urn': 'https://evil.test'}, NOW), (POST, NOW.replace(tzinfo=None))]:
            with self.assertRaises(ValueError):
                signals.due(post, {}, now)
        transport.get.assert_not_called()
        transport.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
