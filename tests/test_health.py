from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from automation.health import build_health, main, summary


NOW = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)  # 15:30 IST
POLICY = {'timezone': 'Asia/Calcutta', 'posting_hours': [9, 18],
          'posting_days': list(range(7)), 'max_posts_per_day': 2,
          'public_engagement_start_hour': 17, 'public_engagement_end_hour': 22}


def receipt(day, hour, status='scheduled', remote_id='verified-group'):
    return {'kind': 'post', 'status': status, 'remote_id': remote_id,
            'allocation_day': day,
            'scheduled_for': f'{day}T{hour:02d}:00:00+05:30',
            'at': NOW.isoformat()}


def ready_state():
    actions = {'post:2026-10-23:0900': receipt('2026-10-23', 9)}
    for day in ('2026-10-24', '2026-10-25', '2026-10-26'):
        for hour in (9, 18):
            actions[f'post:{day}:{hour:02d}00'] = receipt(day, hour)
    return {'actions': actions, 'days': {}, 'operational_status': {}}


class HealthTests(unittest.TestCase):
    def test_actual_failure_shape_cannot_be_green_from_old_validation_or_completed_worker(self):
        state = {'actions': {}, 'days': {'2026-10-22': {'post': 0}, '2026-10-23': {'post': 0}},
                 'operational_status': {
                     'last_live_validation': {'status': 'passed', 'observed_at': '2026-10-12T16:56:41+00:00'},
                     'session': {'status': 'completed', 'heartbeat_at': '2026-10-22T16:28:16+00:00',
                         'recent_errors': [{'phase': 'next-day-posts', 'type': 'ApifyModelError',
                                            'at': '2026-10-22T14:48:48+00:00'}]}}}
        original = deepcopy(state)
        report = build_health(state, POLICY, NOW)
        self.assertEqual(report['status'], 'degraded')
        self.assertFalse(report['posting']['next_day_ready'])
        self.assertEqual(report['posting']['today']['missed_without_intent'], ['post:2026-10-23:0900'])
        self.assertEqual(report['drafting']['recent_errors'][0]['type'], 'ApifyModelError')
        self.assertIn('recent-operation-errors', report['reasons'])
        self.assertEqual(state, original)

    def test_good_three_day_receipts_establish_readiness_without_claiming_publication(self):
        report = build_health(ready_state(), POLICY, NOW)
        self.assertEqual(report['status'], 'ready')
        self.assertTrue(report['posting']['buffer_ready'])
        self.assertEqual([len(day['acknowledged']) for day in report['posting']['buffer']], [2, 2, 2])
        self.assertEqual(report['evidence'], 'local-receipts-only')
        self.assertIn('publication is not reverified', summary(report))

    def test_unknown_rejected_and_missing_ids_are_reserved_but_not_acknowledged(self):
        for status, rid, bucket in [('unknown-needs-reconciliation', None, 'unverified'),
                                    ('inflight', None, 'unverified'), ('rejected', None, 'rejected'),
                                    ('scheduled', None, 'unverified'), ('dry-run', None, 'unverified')]:
            with self.subTest(status=status):
                state = ready_state()
                state['actions']['post:2026-10-24:0900'].update(status=status, remote_id=rid)
                state['days']['2026-10-24'] = {'post': 2}
                report = build_health(state, POLICY, NOW)
                day = report['posting']['buffer'][0]
                self.assertEqual(day[bucket], ['post:2026-10-24:0900'])
                self.assertEqual(len(day['acknowledged']), 1)
                self.assertFalse(report['posting']['next_day_ready'])

    def test_uncertain_today_post_is_not_reported_as_definitely_missed(self):
        state = ready_state()
        state['actions']['post:2026-10-23:0900']['status'] = 'unknown-needs-reconciliation'
        report = build_health(state, POLICY, NOW)
        self.assertEqual(report['posting']['today']['missed_without_intent'], [])
        self.assertEqual(report['posting']['today']['unverified'], ['post:2026-10-23:0900'])
        self.assertEqual(report['status'], 'degraded')

    def test_unacknowledged_today_slot_has_full_two_hour_late_window(self):
        state = ready_state()
        del state['actions']['post:2026-10-23:0900']
        within = NOW.replace(hour=5, minute=30)  # 11:00 IST, still in inclusive slot window
        self.assertEqual(build_health(state, POLICY, within)['posting']['today']['missed_without_intent'], [])
        self.assertEqual(build_health(state, POLICY, within + timedelta(seconds=1))['posting']['today']
                         ['missed_without_intent'], ['post:2026-10-23:0900'])

    def test_legacy_morning_receipt_does_not_hide_missing_evening(self):
        state = ready_state()
        state['actions']['post:2026-10-24'] = state['actions'].pop('post:2026-10-24:0900')
        del state['actions']['post:2026-10-24:1800']
        day = build_health(state, POLICY, NOW)['posting']['buffer'][0]
        self.assertEqual(day['acknowledged'], ['post:2026-10-24:0900'])
        self.assertEqual(day['missing'], ['post:2026-10-24:1800'])

    def test_confirmed_counts_use_sent_receipts_not_daily_allocations(self):
        state = ready_state()
        for key, status, kind, rid in [('like', 'sent', 'reaction', 'ack'),
                ('comment', 'rejected', 'comment', None), ('reply', 'unknown-needs-reconciliation', 'reply', None),
                ('unverified', 'sent', 'comment', None)]:
            state['actions'][key] = {'kind': kind, 'status': status, 'remote_id': rid, 'at': NOW.isoformat()}
        state['days']['2026-10-23'] = {'interaction': 40, 'reply': 10, 'post': 2}
        counts = build_health(state, POLICY, NOW)['interactions']
        self.assertEqual(counts['confirmed_sent'], {'reaction': 1, 'comment': 0, 'reply': 0})
        self.assertEqual(counts['attempts'], {'reaction': 1, 'comment': 2, 'reply': 1})
        self.assertEqual(counts['uncertain'], {'reaction': 0, 'comment': 1, 'reply': 1})
        self.assertEqual(counts['allocated_counters']['interaction'], 40)

    def test_heartbeat_expiry_and_completed_session_during_window_do_not_appear_running(self):
        now = NOW.replace(hour=12)  # 17:30 IST
        for status, stamp, expected in [('running', now - timedelta(minutes=16), 'stale'),
                                        ('completed', now, 'completed')]:
            state = ready_state()
            state['operational_status']['session'] = {'status': status, 'heartbeat_at': stamp.isoformat()}
            report = build_health(state, POLICY, now)
            self.assertEqual(report['session']['status'], expected)
            self.assertIn('engagement-session-not-fresh', report['reasons'])
        state['operational_status']['session']['status'] = 'running'
        self.assertNotIn('engagement-session-not-fresh', build_health(state, POLICY, now)['reasons'])

    def test_analytics_only_cooldown_does_not_report_a_posting_or_reactions_outage(self):
        state = ready_state()
        state['comment_signals'] = {'statistics_cooldown': {'status': 'rate-limited',
            'until': (NOW + timedelta(hours=4)).isoformat(), 'scope': 'application'}}
        report = build_health(state, POLICY, NOW)
        self.assertEqual(report['status'], 'limited')
        self.assertTrue(report['posting']['buffer_ready'])
        self.assertEqual(report['reasons'], [])
        self.assertEqual(report['analytics']['affects'], ['statistics'])
        self.assertFalse(report['analytics']['posting_or_public_writes_blocked'])
        expired = build_health(state, POLICY, NOW + timedelta(hours=4))
        self.assertEqual(expired['analytics']['status'], 'not-currently-held')

    def test_unclaimed_draft_retry_and_latest_deferred_error_are_visible(self):
        state = ready_state()
        del state['actions']['post:2026-10-24:0900']
        state['post_queue'] = {'attempts': {'post:2026-10-24:0900': {
            'attempted_at': NOW.isoformat(), 'retry_after': (NOW + timedelta(hours=2)).isoformat()}}}
        state['operational_status']['queue_preparation'] = {'status': 'deferred',
            'error_type': 'ApifyModelError', 'checked_at': NOW.isoformat(), 'reason': 'private raw provider body'}
        report = build_health(state, POLICY, NOW)
        self.assertEqual(report['drafting']['unclaimed_attempts'][0]['status'], 'waiting-for-retry')
        self.assertEqual(report['drafting']['latest_error_type'], 'ApifyModelError')
        self.assertNotIn('private raw provider body', json.dumps(report))

    def test_cli_default_is_read_only_and_required_next_day_buffer_changes_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path, policy_path = Path(directory) / 'state.json', Path(directory) / 'policy.json'
            state_path.write_text(json.dumps({'actions': {}}), encoding='utf-8')
            policy_path.write_text(json.dumps(POLICY), encoding='utf-8')
            original = state_path.read_bytes()
            args = ['--state', str(state_path), '--policy', str(policy_path)]
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(main(args), 0)
                self.assertEqual(json.loads(output.getvalue())['status'], 'degraded')
            with patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(main(args + ['--summary', '--require-buffer']), 2)
                self.assertIn('Autopilot health', output.getvalue())
            self.assertEqual(state_path.read_bytes(), original)

    def test_naive_clocks_fail_and_future_or_old_session_errors_do_not_control_current_health(self):
        with self.assertRaises(ValueError):
            build_health(ready_state(), POLICY, NOW.replace(tzinfo=None))
        state = ready_state()
        state['operational_status']['session'] = {'status': 'completed', 'recent_errors': [
            {'phase': 'post', 'type': 'ApifyModelError', 'at': (NOW - timedelta(days=2)).isoformat()},
            {'phase': 'post', 'type': 'ApifyModelError', 'at': (NOW + timedelta(days=1)).isoformat()}]}
        self.assertEqual(build_health(state, POLICY, NOW)['status'], 'ready')


if __name__ == '__main__':
    unittest.main()
