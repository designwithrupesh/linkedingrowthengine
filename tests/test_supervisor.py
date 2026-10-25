"""Bounded handoff workers preserve session pacing and checkpoint failures."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from automation.supervisor import CHECKPOINT_EXIT_CODE, run_once, run_supervisor
from automation.session import SessionCheckpointError, SessionSourceUpdated, session_bounds
from automation.twin import GitCheckpointError


POLICY = {'timezone': 'Asia/Calcutta', 'public_engagement_start_hour': 17,
          'public_engagement_end_hour': 22}
START = datetime(2026, 10, 23, 11, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self.now = now
        self.waits = []
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.clock = Clock(START)
        self.refresh = Mock()
        self.oneshot_calls = []
        self.session_calls = []
        self.saved = []
        self.oneshot_hook = None
        self.session_hook = None
        self.save_hook = None
        self.enterContext(patch('builtins.print'))

    def runner_factory(self, policy, path, *, live, now):
        owner = self
        class ProgressRunner:
            def __init__(self):
                self.state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            def save(self):
                if owner.save_hook:
                    owner.save_hook()
                path.write_text(json.dumps(self.state), encoding="utf-8")
                owner.saved.append(json.loads(json.dumps(self.state)))
        self.assertTrue(live)
        return ProgressRunner()

    def one_shot(self, path, *, timeout_seconds):
        self.oneshot_calls.append((self.clock(), path, timeout_seconds))
        if self.oneshot_hook:
            self.oneshot_hook()

    def session(self, policy, path, **kwargs):
        self.session_calls.append((self.clock(), kwargs))
        self.assertEqual(kwargs['poll_seconds'], 300)
        if self.session_hook:
            return self.session_hook()
        bounds = session_bounds(self.clock(), policy, max_minutes=kwargs['max_minutes'])
        if bounds:
            self.clock.now = bounds[1]
        return {'status': 'completed'}

    def run_worker(self, **kwargs):
        return run_supervisor(POLICY, self.path, clock=self.clock, sleeper=self.clock.sleep,
                              one_shot=self.one_shot, session=self.session,
                              refresh=self.refresh, runner_factory=self.runner_factory, **kwargs)

    def test_pre_window_maintenance_wakes_at_window_start_then_uses_session(self):
        self.clock.now = START - timedelta(minutes=10)
        result = self.run_worker(max_minutes=30)
        self.assertEqual([stamp for stamp, _, _ in self.oneshot_calls], [START - timedelta(minutes=10)])
        self.assertEqual(self.session_calls[0][0], START)
        self.assertEqual(self.session_calls[0][1]['max_minutes'], 20)
        self.assertEqual(self.clock(), START + timedelta(minutes=20))
        self.assertEqual(result, {'status': 'completed', 'cycles': 2, 'sessions': 1,
                                  'recent_errors': []})
        self.assertTrue(all(wait <= 60 for wait in self.clock.waits))
        self.assertEqual(self.saved[-1]['operational_status']['supervisor']['phase'], 'handoff')

    def test_five_hour_budget_is_shared_across_waiting_and_active_session(self):
        self.clock.now = START - timedelta(hours=1)
        deadline = self.clock() + timedelta(minutes=300)
        result = self.run_worker()
        self.assertEqual(len(self.oneshot_calls), 4)
        self.assertEqual(self.session_calls[0][0], START)
        self.assertEqual(self.session_calls[0][1]['max_minutes'], 240)
        self.assertEqual(self.clock(), deadline)
        self.assertEqual(result['sessions'], 1)

    def test_slow_maintenance_child_yields_at_engagement_boundary(self):
        self.clock.now = START - timedelta(minutes=10)
        def slow_child():
            timeout = self.oneshot_calls[-1][2]
            self.assertEqual(timeout, 600)
            self.clock.sleep(timeout)
            # An intent can already be durable when a request times out.
            state = json.loads(self.path.read_text(encoding='utf-8'))
            state['actions'] = {'reserved': {'status': 'inflight'}}
            self.path.write_text(json.dumps(state), encoding='utf-8')
            raise subprocess.TimeoutExpired('worker', timeout)
        self.oneshot_hook = slow_child
        result = self.run_worker(max_minutes=30)
        self.assertEqual(len(self.oneshot_calls), 1)
        self.assertEqual(self.session_calls[0][0], START)
        self.assertEqual(self.session_calls[0][1]['max_minutes'], 20)
        self.assertEqual(result['recent_errors'][0]['type'], 'TimeoutExpired')
        self.assertEqual(self.saved[-1]['actions']['reserved']['status'], 'inflight')
        # Failed-child checkpoint verification precedes the session's refresh.
        self.assertGreaterEqual(self.refresh.call_count, 3)

    def test_after_window_uses_only_normal_intent_aware_cycles(self):
        self.clock.now = START + timedelta(hours=5)
        self.run_worker(max_minutes=30)
        self.assertEqual(len(self.oneshot_calls), 2)
        self.assertFalse(self.session_calls)
        self.assertTrue(all(wait <= 60 for wait in self.clock.waits))

    def test_session_ends_at_window_close_and_maintenance_resumes(self):
        self.clock.now = START + timedelta(hours=4, minutes=50)
        self.run_worker(max_minutes=30)
        self.assertEqual(len(self.session_calls), 1)
        self.assertEqual(self.session_calls[0][0], START + timedelta(hours=4, minutes=50))
        self.assertEqual(self.oneshot_calls[0][0], START + timedelta(hours=5))
        self.assertEqual(len(self.oneshot_calls), 2)
        self.assertEqual(self.clock(), START + timedelta(hours=5, minutes=20))

    def test_ordinary_cycle_error_waits_for_normal_poll_and_continues(self):
        self.clock.now = START - timedelta(hours=2)
        self.oneshot_hook = Mock(side_effect=[RuntimeError('private response body'), None])
        result = self.run_worker(max_minutes=30)
        self.assertEqual(len(self.oneshot_calls), 2)
        self.assertEqual(self.oneshot_calls[1][0] - self.oneshot_calls[0][0], timedelta(minutes=15))
        self.assertEqual(result['recent_errors'][0]['type'], 'RuntimeError')
        self.assertNotIn('private response body', str(self.saved))

    def test_ordinary_session_error_waits_for_five_minute_poll(self):
        self.session_hook = Mock(side_effect=[RuntimeError('provider'), None])
        self.run_worker(max_minutes=10)
        self.assertEqual([stamp for stamp, _ in self.session_calls],
                         [START, START + timedelta(minutes=5)])
        self.assertFalse(self.oneshot_calls)

    def test_git_checkpoint_failure_is_fatal_without_later_cycle(self):
        self.clock.now = START - timedelta(hours=2)
        self.oneshot_hook = Mock(side_effect=GitCheckpointError('failed checkpoint'))
        with self.assertRaises(GitCheckpointError):
            self.run_worker(max_minutes=30)
        self.assertEqual(len(self.oneshot_calls), 1)
        self.assertFalse(self.clock.waits)
        self.assertEqual(len(self.saved), 1)

    def test_failed_child_with_unpushed_intent_stops_before_progress_or_later_work(self):
        self.clock.now = START - timedelta(hours=2)
        self.oneshot_hook = Mock(side_effect=RuntimeError('worker failed'))
        self.refresh.side_effect = [None, SessionCheckpointError('unpushed intent')]
        with self.assertRaises(SessionCheckpointError):
            self.run_worker(max_minutes=30)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.oneshot_calls), 1)

    def test_progress_checkpoint_failure_prevents_any_worker_start(self):
        self.save_hook = Mock(side_effect=RuntimeError('save failed'))
        with self.assertRaises(SessionCheckpointError):
            self.run_worker(max_minutes=10)
        self.assertFalse(self.session_calls)
        self.assertFalse(self.oneshot_calls)

    def test_source_change_returns_for_fresh_checkout_without_stale_save(self):
        self.clock.now = START - timedelta(hours=2)
        self.refresh.side_effect = [None, SessionSourceUpdated('new source')]
        result = self.run_worker(max_minutes=30)
        self.assertEqual(result['status'], 'source-updated')
        self.assertEqual(len(self.oneshot_calls), 1)
        self.assertEqual(len(self.saved), 1)

    def test_source_change_inside_session_hands_off_without_a_stale_heartbeat(self):
        self.session_hook = Mock(side_effect=SessionSourceUpdated('new source'))
        result = self.run_worker(max_minutes=30)
        self.assertEqual(result['status'], 'source-updated')
        self.assertEqual(len(self.session_calls), 1)
        self.assertEqual(len(self.saved), 1)

    def test_session_checkpoint_failure_does_not_restart_the_session(self):
        self.session_hook = Mock(side_effect=SessionCheckpointError('unpublished checkpoint'))
        with self.assertRaises(SessionCheckpointError):
            self.run_worker(max_minutes=30)
        self.assertEqual(len(self.session_calls), 1)
        self.assertFalse(self.clock.waits)

    def test_slow_heartbeat_cannot_start_worker_after_the_total_budget(self):
        self.clock.now = START - timedelta(hours=2)
        self.save_hook = lambda: self.clock.sleep(60)
        result = self.run_worker(max_minutes=1)
        self.assertEqual(result['cycles'], 0)
        self.assertFalse(self.oneshot_calls)
        self.assertFalse(self.session_calls)

    def test_child_checkpoint_is_reloaded_before_supervisor_heartbeat(self):
        self.clock.now = START - timedelta(hours=2)
        def write_child_receipt():
            state = json.loads(self.path.read_text(encoding="utf-8"))
            state['child_receipt'] = {'status': 'sent'}
            self.path.write_text(json.dumps(state), encoding="utf-8")
        self.oneshot_hook = write_child_receipt
        self.run_worker(max_minutes=1)
        self.assertEqual(self.saved[-1]['child_receipt'], {'status': 'sent'})

    def test_malformed_bounds_and_poll_settings_are_rejected_without_work(self):
        for kwargs in ({'max_minutes': 0}, {'max_minutes': 301}, {'max_minutes': True},
                       {'poll_seconds': 59}, {'poll_seconds': 901}, {'poll_seconds': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_worker(**kwargs)
        self.clock.now = START.replace(tzinfo=None)
        with self.assertRaises(ValueError):
            self.run_worker()
        self.assertFalse(self.oneshot_calls)


class OneShotTests(unittest.TestCase):
    @patch('automation.supervisor.subprocess.run')
    def test_fixed_checkpoint_exit_is_fatal_without_subprocess_retry(self, run):
        run.return_value = subprocess.CompletedProcess([], CHECKPOINT_EXIT_CODE)
        with self.assertRaises(SessionCheckpointError):
            run_once(Path('/tmp/state.json'), timeout_seconds=59)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:],
                         ['-m', 'automation.twin', '--state', '/tmp/state.json'])
        self.assertEqual(run.call_args.kwargs['timeout'], 59)

    @patch('automation.supervisor.subprocess.run')
    def test_normal_failure_and_timeout_are_not_immediately_retried(self, run):
        run.return_value = subprocess.CompletedProcess([], 1)
        with self.assertRaises(RuntimeError):
            run_once(Path('/tmp/state.json'), timeout_seconds=59)
        self.assertEqual(run.call_count, 1)
        run.reset_mock()
        run.side_effect = subprocess.TimeoutExpired('worker', 59)
        with self.assertRaises(subprocess.TimeoutExpired):
            run_once(Path('/tmp/state.json'), timeout_seconds=59)
        self.assertEqual(run.call_count, 1)
