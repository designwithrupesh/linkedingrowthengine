"""Durable checkpoint recovery using real, isolated Git repositories."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from automation.session import SessionCheckpointError, run_session
from automation.twin import GitCheckpointError, Runner


REAL_RUN = subprocess.run
NOW = datetime(2026, 10, 12, 11, 30, tzinfo=timezone.utc)
POLICY = {
    'timezone': 'Asia/Calcutta', 'platform_id': 'test-platform',
    'max_posts_per_day': 1, 'max_interactions_per_day': 5,
}


class CheckpointGitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.remote = self.base / 'remote.git'
        self.local = self.base / 'worker'
        self.local.mkdir()
        self.git('init', '--bare', str(self.remote))
        self.git('init', '-b', 'main', str(self.local))
        self.configure(self.local)
        (self.local / 'README.md').write_text('Initial source.\n', encoding='utf-8')
        self.git('add', 'README.md')
        self.commit('Initial source')
        self.git('remote', 'add', 'origin', str(self.remote))
        self.git('push', 'origin', 'HEAD:refs/heads/main')
        self.initial = self.remote_head()
        self.path = self.local / 'automation/state.json'
        self.enterContext(patch('automation.twin.ROOT', self.local))
        self.enterContext(patch('automation.twin.sleep'))
        self.enterContext(patch.dict(os.environ, {'TWIN_CHECKPOINT_GIT': 'true'}))
        self.runner = Runner(POLICY, self.path, live=True, now=NOW)
        self.pushes = []

    def git(self, *args, cwd=None):
        return REAL_RUN(['git', *args], cwd=cwd or self.local, check=True,
                        capture_output=True, text=True, timeout=15).stdout.strip()

    def configure(self, path):
        self.git('config', 'user.name', 'Sergey Bulaev', cwd=path)
        self.git('config', 'user.email', 's@bulaev.org', cwd=path)

    def commit(self, message, cwd=None):
        self.git('commit', '--author=Sergey Bulaev <s@bulaev.org>', '-m', message, cwd=cwd)

    def remote_head(self):
        return self.git('--git-dir=' + str(self.remote), 'rev-parse', 'refs/heads/main')

    def failed_push(self, command):
        return subprocess.CompletedProcess(command, 1, '', 'remote: fatal error in commit_refs; private diagnostic')

    def intercept(self, push_behavior):
        def run(command, *args, **kwargs):
            if command[:2] == ['git', 'push']:
                self.pushes.append(command)
                return push_behavior(command, args, kwargs)
            return REAL_RUN(command, *args, **kwargs)
        return patch('automation.twin.subprocess.run', side_effect=run)

    def test_transient_ref_update_failure_recovers_same_checkpoint_without_recommit(self):
        def push(command, args, kwargs):
            return self.failed_push(command) if len(self.pushes) == 1 else REAL_RUN(command, *args, **kwargs)
        self.runner.state['checkpoint_test'] = 'receipt'
        with self.intercept(push):
            self.runner.save()
        self.assertEqual(len(self.pushes), 2)
        self.assertEqual(self.pushes[0], self.pushes[1])
        self.assertEqual(self.pushes[0][-1], self.remote_head() + ':refs/heads/main')
        self.assertEqual(self.git('rev-list', '--count', self.initial + '..HEAD'), '1')
        receipt = self.git('--git-dir=' + str(self.remote), 'show', 'main:automation/state.json')
        self.assertEqual(json.loads(receipt)['checkpoint_test'], 'receipt')

    def test_lost_push_ack_is_confirmed_by_remote_without_repeating_push(self):
        def push(command, args, kwargs):
            REAL_RUN(command, *args, **kwargs)
            raise subprocess.TimeoutExpired(command, 30, stderr='private transport detail')
        with self.intercept(push):
            self.runner.save()
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.remote_head(), self.git('rev-parse', 'HEAD'))

    def test_remote_divergence_stops_without_force_push_or_merging_receipts(self):
        writer = self.base / 'other-writer'
        self.git('clone', '--branch', 'main', str(self.remote), str(writer))
        self.configure(writer)
        newer = []
        def push(command, args, kwargs):
            (writer / 'README.md').write_text('Concurrent source change.\n', encoding='utf-8')
            self.git('add', 'README.md', cwd=writer)
            self.commit('Concurrent source', cwd=writer)
            self.git('push', 'origin', 'HEAD:refs/heads/main', cwd=writer)
            newer.append(self.remote_head())
            return self.failed_push(command)
        with self.intercept(push), self.assertRaisesRegex(GitCheckpointError, 'diverged'):
            self.runner.save()
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.remote_head(), newer[0])
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), newer[0])
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['actions'], {})

    def test_three_failed_pushes_leave_local_checkpoint_and_private_diagnostics_are_hidden(self):
        with self.intercept(lambda command, *_: self.failed_push(command)), \
             self.assertRaises(GitCheckpointError) as caught:
            self.runner.save()
        self.assertEqual(len(self.pushes), 3)
        self.assertEqual(len({tuple(command) for command in self.pushes}), 1)
        self.assertEqual(self.remote_head(), self.initial)
        self.assertNotEqual(self.git('rev-parse', 'HEAD'), self.initial)
        self.assertNotIn('private diagnostic', str(caught.exception))

    def test_unchanged_index_recovers_prior_unpushed_checkpoint_instead_of_claiming_success(self):
        with self.intercept(lambda command, *_: self.failed_push(command)), self.assertRaises(GitCheckpointError):
            self.runner.save()
        checkpoint = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.pushes.clear()
        with self.intercept(lambda command, args, kwargs: REAL_RUN(command, *args, **kwargs)):
            self.runner.save()
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.pushes[0][-1], checkpoint + ':refs/heads/main')
        self.assertEqual(self.remote_head(), checkpoint)
        self.assertEqual(self.git('rev-parse', 'HEAD'), checkpoint)

    def test_unchanged_index_acknowledged_remote_needs_no_push(self):
        self.runner.save()
        with self.intercept(lambda *_: self.fail('An acknowledged unchanged checkpoint must not push')):
            self.runner.save()
        self.assertEqual(self.pushes, [])

    def test_unverifiable_remote_after_failed_push_prevents_another_attempt(self):
        def run(command, *args, **kwargs):
            if command[:2] == ['git', 'push']:
                self.pushes.append(command)
                return self.failed_push(command)
            if command[:2] == ['git', 'fetch']:
                raise subprocess.TimeoutExpired(command, 30)
            return REAL_RUN(command, *args, **kwargs)
        with patch('automation.twin.subprocess.run', side_effect=run), self.assertRaises(GitCheckpointError):
            self.runner.save()
        self.assertEqual(len(self.pushes), 1)

    def test_final_checkpoint_failure_stops_session_before_later_linkedin_writes(self):
        later = Mock()
        def factory(policy, path, *, live, now):
            runner = Runner(policy, path, live=live, now=now)
            runner.post = lambda: runner.write('first-intent', 'reaction', 'LIKE', 'urn:li:share:7000000000000000000')
            runner.discover_published = later
            runner.private_workflow = later
            return runner
        with self.intercept(lambda command, *_: self.failed_push(command)), \
             patch.dict(os.environ, {'TWIN_READ_ENABLED': 'false'}), \
             patch('automation.twin.requests.post') as send, \
             self.assertRaises(SessionCheckpointError):
            run_session(POLICY, self.path, max_minutes=10, clock=lambda: NOW,
                        runner_factory=factory, verify=Mock(), refresh=Mock())
        self.assertEqual(len(self.pushes), 3)
        send.assert_not_called()
        later.assert_not_called()
        receipt = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(receipt['actions']['first-intent']['status'], 'inflight')
        self.assertEqual(self.remote_head(), self.initial)


if __name__ == '__main__':
    unittest.main()
