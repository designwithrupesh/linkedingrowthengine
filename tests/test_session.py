import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone

from automation.session import (SessionCheckpointError, SessionSourceUpdated,
                                refresh_checkpoint, run_session, session_bounds)
from automation.twin import LocalModelError, WriteOutcomeError
from automation.private_actions import PrivateWriteOutcomeError


POLICY = {
    'timezone': 'Asia/Calcutta',
    'public_engagement_start_hour': 17,
    'public_engagement_end_hour': 22,
    'public_engagement_slot_minutes': 15,
    'max_public_interactions_per_day': 40,
    'max_public_actions_per_session': 2,
}
START = datetime(2026, 10, 10, 11, 30, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self.now = now
        self.waits = []
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.clock = Clock(START)
        self.calls = []
        self.writes = []
        self.runners = []
        self.hooks = {}
        self.verify = Mock()
        self.refresh = Mock()
        self.queue = self.enterContext(patch('automation.session.prepare_next_day_posts'))
        self.enterContext(patch('builtins.print'))
        self.enterContext(patch.dict(os.environ, {'TWIN_READ_ENABLED': 'true'}))

    def factory(self, policy, path, live, now):
        owner = self
        class FakeRunner:
            def __init__(self):
                self.policy = policy
                self.now = now
                self.state = (json.loads(path.read_text(encoding='utf-8')) if path.exists()
                              else {'actions': {}, 'days': {}})
                self.local = now
                self.day = '2026-10-10'
                self.counts = self.state['days'].setdefault(self.day, {})
                self.cycle = len(owner.runners)
            def save(self):
                hook = owner.hooks.get('save')
                if hook:
                    hook(self)
                path.write_text(json.dumps(self.state), encoding='utf-8')
            def phase(self, name):
                owner.calls.append((self.cycle, name))
                if hook := owner.hooks.get(name):
                    hook(self)
            def post(self):
                self.phase('post')
            def discover_published(self):
                self.phase('published-posts')
            def replies(self, reader):
                self.phase('replies')
            def private_workflow(self):
                self.phase('private')
            def public_engagement(self, reader):
                self.phase('public')
                self.write(f'like:{self.cycle}', 'reaction', 'LIKE')
                self.write(f'comment:{self.cycle}', 'comment', 'A specific observation about this design decision.')
            def analytics(self, reader):
                self.phase('analytics')
            def write(self, key, kind, text, *args, **kwargs):
                if key in self.state['actions']:
                    return
                self.state['actions'][key] = {'kind': kind, 'at': self.now.isoformat(),
                                              'status': 'sent', 'text': text}
                self.counts['interaction'] = self.counts.get('interaction', 0) + 1
                owner.writes.append(self.state['actions'][key])
                self.save()
        runner = FakeRunner()
        self.runners.append(runner)
        return runner

    def run_worker(self, **kwargs):
        return run_session(POLICY, self.path, clock=self.clock, sleeper=self.clock.sleep,
                           runner_factory=self.factory, reader_factory=Mock,
                           verify=self.verify, refresh=self.refresh, **kwargs)

    def test_five_hour_session_keeps_replies_running_and_paces_twenty_pairs(self):
        result = self.run_worker()
        self.assertEqual(result['cycles'], 60)
        self.assertEqual(len(self.writes), 40)
        self.assertEqual(sum(a['kind'] == 'comment' for a in self.writes), 20)
        self.assertEqual(sum(name == 'replies' for _, name in self.calls), 60)
        self.verify.assert_called_once_with(POLICY)
        self.assertEqual(self.refresh.call_count, 60)
        self.assertEqual(self.queue.call_count, 60)
        self.assertTrue(all(seconds <= 60 for seconds in self.clock.waits))
        state = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(state['operational_status']['session']['status'], 'completed')
        self.assertEqual(self.clock.now, START + timedelta(hours=5))

    def test_pre_window_wait_runs_only_today_without_missing_tail(self):
        self.clock.now -= timedelta(minutes=15)
        result = self.run_worker(wait_for_window=True)
        self.assertEqual(result['cycles'], 60)
        self.assertEqual(self.writes[0]['at'], START.isoformat())
        self.assertEqual(self.clock.now, START + timedelta(hours=5))

    def test_late_start_never_backfills_missed_public_slots(self):
        self.clock.now = START + timedelta(hours=4, minutes=30)
        result = self.run_worker()
        self.assertEqual(result['cycles'], 6)
        self.assertEqual(len(self.writes), 4)

    def test_generation_failure_does_not_suppress_replies_or_other_phases(self):
        self.hooks['post'] = Mock(side_effect=LocalModelError('Local model request failed'))
        result = self.run_worker(max_minutes=10)
        self.assertEqual(result['cycles'], 2)
        self.assertEqual(sum(name == 'replies' for _, name in self.calls), 2)
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(self.hooks['post'].call_count, 2)
        self.assertEqual(result['recent_errors'][-1]['phase'], 'post')

    def test_checkpoint_failure_stops_before_any_later_phase_or_poll(self):
        self.hooks['save'] = Mock(side_effect=RuntimeError('Checkpoint failure'))
        self.hooks['post'] = lambda runner: runner.save()
        with self.assertRaises(SessionCheckpointError):
            self.run_worker(max_minutes=10)
        self.assertEqual(self.calls, [(0, 'post')])
        self.assertEqual(len(self.runners), 1)
        self.assertFalse(self.writes)

    def test_checkpoint_failure_wrapped_as_write_outcome_still_stops_every_stage(self):
        self.hooks['save'] = Mock(side_effect=RuntimeError('Checkpoint failure'))
        def translated_failure(runner):
            try:
                runner.save()
            except SessionCheckpointError:
                raise WriteOutcomeError('The uncertain write checkpoint failed') from None
        self.hooks['post'] = translated_failure
        with self.assertRaises(SessionCheckpointError):
            self.run_worker(max_minutes=10)
        self.assertEqual(self.calls, [(0, 'post')])
        self.assertEqual(len(self.runners), 1)
        self.assertFalse(self.writes)

    def test_private_checkpoint_failure_wrapped_as_outcome_stops_public_writes(self):
        self.hooks['save'] = Mock(side_effect=RuntimeError('Checkpoint failure'))
        def translated_failure(runner):
            try:
                runner.save()
            except SessionCheckpointError:
                raise PrivateWriteOutcomeError('The private checkpoint failed') from None
        self.hooks['private'] = translated_failure
        with self.assertRaises(SessionCheckpointError):
            self.run_worker(max_minutes=10)
        self.assertEqual([name for _, name in self.calls],
                         ['post', 'published-posts', 'replies', 'private'])
        self.assertFalse(self.writes)

    def test_checkpoint_failure_cannot_be_swallowed_before_another_write(self):
        self.hooks['save'] = Mock(side_effect=RuntimeError('Checkpoint failure'))
        def swallowed_failure(runner):
            try:
                runner.save()
            except SessionCheckpointError:
                pass
            runner.write('unsafe-later-write', 'reaction', 'LIKE')
        self.hooks['post'] = swallowed_failure
        with self.assertRaises(SessionCheckpointError):
            self.run_worker(max_minutes=10)
        self.assertFalse(self.writes)
        self.assertNotIn('unsafe-later-write', self.runners[0].state['actions'])

    def test_swallowed_checkpoint_failure_stops_before_the_next_phase(self):
        self.hooks['save'] = Mock(side_effect=RuntimeError('Checkpoint failure'))
        def swallowed_failure(runner):
            try:
                runner.save()
            except SessionCheckpointError:
                pass
        self.hooks['post'] = swallowed_failure
        with self.assertRaises(SessionCheckpointError):
            self.run_worker(max_minutes=10)
        self.assertEqual(self.calls, [(0, 'post')])
        self.assertFalse(self.writes)

    def test_source_change_stops_before_next_cycle(self):
        self.refresh.side_effect = [None, SessionSourceUpdated('Changed')]
        with self.assertRaises(SessionSourceUpdated):
            self.run_worker(max_minutes=10)
        self.assertEqual(len(self.runners), 1)
        self.assertEqual(len(self.writes), 2)

    def test_fresh_checkpoint_is_reloaded_for_every_cycle(self):
        def add_remote_receipt():
            if self.path.exists():
                state = json.loads(self.path.read_text(encoding='utf-8'))
                state['external_checkpoint'] = 'preserved'
                self.path.write_text(json.dumps(state), encoding='utf-8')
        self.refresh.side_effect = add_remote_receipt
        self.run_worker(max_minutes=10)
        self.assertEqual(self.runners[1].state['external_checkpoint'], 'preserved')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['external_checkpoint'], 'preserved')

    def test_draft_finishing_after_window_creates_no_write_intent(self):
        self.clock.now = START + timedelta(hours=4, minutes=59)
        self.hooks['public'] = lambda runner: self.clock.sleep(120)
        result = self.run_worker()
        self.assertEqual(result['cycles'], 1)
        self.assertFalse(self.writes)
        self.assertFalse(self.runners[0].state['actions'])

    def test_draft_crossing_slot_uses_actual_write_timestamp(self):
        self.clock.now = START + timedelta(minutes=14, seconds=50)
        self.hooks['public'] = lambda runner: self.clock.sleep(20)
        self.run_worker(max_minutes=1)
        self.assertEqual(self.writes[0]['at'], (START + timedelta(minutes=15, seconds=10)).isoformat())

    def test_uncertain_public_attempt_still_consumes_current_slot_allowance(self):
        self.path.write_text(json.dumps({'actions': {'uncertain': {
            'kind': 'reaction', 'at': START.isoformat(),
            'status': 'unknown-needs-reconciliation'}}, 'days': {}}), encoding='utf-8')
        self.run_worker(max_minutes=10)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.runners[1].state['actions']['uncertain']['status'],
                         'unknown-needs-reconciliation')

    def test_outside_window_does_not_verify_connect_or_publish(self):
        self.clock.now = START + timedelta(hours=5)
        result = self.run_worker()
        self.assertEqual(result['status'], 'outside-window')
        self.verify.assert_not_called()
        self.refresh.assert_not_called()
        self.assertFalse(self.runners)

    def test_bounds_reject_naive_clock_and_excessive_duration(self):
        with self.assertRaises(ValueError):
            session_bounds(START.replace(tzinfo=None), POLICY)
        with self.assertRaises(ValueError):
            session_bounds(START, POLICY, max_minutes=331)
        self.assertIsNone(session_bounds(START - timedelta(hours=2), POLICY,
                                         wait_for_window=True))


class CheckpointRefreshTests(unittest.TestCase):
    def git_results(self, values):
        return [subprocess.CompletedProcess([], 0, stdout=value, stderr='') for value in values]

    @patch('automation.session.subprocess.run')
    def test_remote_state_only_changes_fast_forward(self, run):
        run.side_effect = self.git_results(['', '', '0\t1', 'automation/state.json', ''])
        refresh_checkpoint()
        self.assertEqual(run.call_args_list[-1].args[0],
                         ['git', 'merge', '--ff-only', 'refs/remotes/origin/main'])

    @patch('automation.session.subprocess.run')
    def test_source_change_never_merges_into_running_worker(self, run):
        run.side_effect = self.git_results(['', '', '0\t1', 'automation/twin.py'])
        with self.assertRaises(SessionSourceUpdated):
            refresh_checkpoint()
        self.assertEqual(run.call_count, 4)

    @patch('automation.session.subprocess.run')
    def test_unpushed_checkpoint_stops_new_remote_actions(self, run):
        run.side_effect = self.git_results(['', '', '1\t0'])
        with self.assertRaises(SessionCheckpointError):
            refresh_checkpoint()

    @patch('automation.session.subprocess.run')
    def test_tracked_changes_are_not_overwritten(self, run):
        run.side_effect = self.git_results([' M automation/state.json'])
        with self.assertRaises(SessionCheckpointError):
            refresh_checkpoint()
        self.assertEqual(run.call_count, 1)


if __name__ == '__main__':
    unittest.main()
