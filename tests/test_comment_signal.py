from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock

import requests

from automation.comment_signal import CommentSignals, FALLBACK_SECONDS, STATISTICS_URL


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
