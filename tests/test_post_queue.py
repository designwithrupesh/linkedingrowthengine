from datetime import datetime, timedelta, timezone
import ast
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from automation.post_queue import prepare_next_day_posts
from automation.twin import Runner
from automation import twin


ZONE = ZoneInfo('Asia/Calcutta')
NOW = datetime(2026, 10, 10, 20, 30, tzinfo=ZONE).astimezone(timezone.utc)
TEXT = ('A product can ask for the right information and still make people hesitate. '
        'If the first screen asks for a company name, team size and role, I would '
        'check whether those answers change the next step. Otherwise the user '
        'is doing work before seeing what the product helps them do.\n\n'
        'Start by showing one useful task they can finish. Ask for the details '
        'when they make that task easier. That gives each question a purpose '
        'and gives the team a clearer way to judge whether onboarding works.')
POLICY = {'timezone': 'Asia/Calcutta', 'platform_id': 'linkedin-test',
          'posting_hours': [9, 18], 'posting_days': list(range(7)),
          'max_posts_per_day': 2, 'topics': ['product design', 'brand strategy'],
          'background': ['product design', 'creative direction'], 'source_notes': [],
          'max_interactions_per_day': 40}


class PostQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'

    def runner(self, **kwargs):
        return Runner(dict(POLICY), self.path, now=NOW,
                      generate=Mock(return_value={'text': TEXT, 'skip': False}),
                      capacity=Mock(return_value=(True, 'verified')), **kwargs)

    def rolling_runner(self, **kwargs):
        runner = self.runner(**kwargs)
        runner.policy.update(posting_buffer_days=3, visual_formats_by_weekday=['text'] * 7)
        return runner

    def test_three_day_buffer_fills_two_slots_per_run_and_survives_restarts(self):
        with patch('automation.post_queue.ROOT', Path(self.temp.name)), \
             patch('automation.twin.requests.post') as publish:
            for day in ('2026-10-11', '2026-10-12', '2026-10-13'):
                runner = self.rolling_runner()
                self.assertEqual(prepare_next_day_posts(runner),
                                 [f'post:{day}:0900', f'post:{day}:1800'])
                self.assertEqual(runner.generate.call_count, 2)
            restarted = self.rolling_runner()
            self.assertEqual(prepare_next_day_posts(restarted), [])
        restarted.generate.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(len(restarted.state['actions']), 6)
        self.assertEqual([restarted.state['days'][day]['post'] for day in
                          ('2026-10-11', '2026-10-12', '2026-10-13')], [2, 2, 2])
        self.assertNotIn('2026-10-14', restarted.state['days'])
        self.assertEqual(restarted.counts['post'], 0)

    def test_pre_window_run_schedules_todays_evening_at_exact_nominal_time(self):
        runner = self.rolling_runner()
        runner.now = datetime(2026, 10, 23, 17, 30, tzinfo=ZONE).astimezone(timezone.utc)
        runner.local = runner.now.astimezone(ZONE)
        runner.day = runner.local.date().isoformat()
        runner.counts = runner.state['days'].setdefault(runner.day, {'post': 0, 'interaction': 0, 'read': 0})
        with patch('automation.post_queue.ROOT', Path(self.temp.name)):
            self.assertEqual(prepare_next_day_posts(runner),
                             ['post:2026-10-23:1800', 'post:2026-10-24:0900'])
        scheduled = runner.state['actions']['post:2026-10-23:1800']
        self.assertEqual(scheduled['scheduled_for'], '2026-10-23T12:30:00+00:00')
        self.assertEqual(scheduled['allocation_day'], '2026-10-23')
        self.assertNotIn('post:2026-10-23:0900', runner.state['actions'])
        self.assertEqual(runner.counts['post'], 1)
        restored = json.loads(runner.path.read_text(encoding='utf-8'))
        self.assertEqual(restored['actions']['post:2026-10-23:1800']['scheduled_for'], scheduled['scheduled_for'])

    def test_exact_or_past_nominal_slot_is_not_prepared_as_a_late_publication(self):
        for minute in (0, 1):
            runner = self.rolling_runner()
            runner.now = datetime(2026, 10, 23, 18, minute, tzinfo=ZONE).astimezone(timezone.utc)
            runner.path = self.path.with_name(f'nominal-{minute}.json')
            with self.subTest(minute=minute), patch('automation.post_queue.ROOT', Path(self.temp.name)):
                self.assertEqual(prepare_next_day_posts(runner),
                                 ['post:2026-10-24:0900', 'post:2026-10-24:1800'])
            self.assertNotIn('post:2026-10-23:1800', runner.state['actions'])

    def test_buffer_skips_excluded_dates_and_retains_each_days_quota(self):
        runner = self.rolling_runner()
        runner.policy.update(posting_days=[0, 1, 2, 3, 4], max_posts_per_day=1)
        with patch('automation.post_queue.ROOT', Path(self.temp.name)):
            self.assertEqual(prepare_next_day_posts(runner),
                             ['post:2026-10-12:0900', 'post:2026-10-13:0900'])
        self.assertNotIn('2026-10-11', runner.state['days'])
        self.assertEqual(runner.state['days']['2026-10-12']['post'], 1)
        self.assertEqual(runner.state['days']['2026-10-13']['post'], 1)

    def test_full_or_uncertain_first_day_does_not_block_later_buffer_dates(self):
        for status in ('scheduled', 'inflight', 'unknown-needs-reconciliation', 'rejected'):
            with self.subTest(status=status):
                runner = self.rolling_runner()
                runner.path = self.path.with_name('state-' + status + '.json')
                runner.state['actions'] = {
                    'post:2026-10-11:0900': {'kind': 'post', 'status': status},
                    'post:2026-10-11:1800': {'kind': 'post', 'status': status},
                }
                with patch('automation.post_queue.ROOT', Path(self.temp.name)):
                    self.assertEqual(prepare_next_day_posts(runner),
                                     ['post:2026-10-12:0900', 'post:2026-10-12:1800'])
                self.assertEqual(runner.generate.call_count, 2)
                self.assertEqual(runner.state['actions']['post:2026-10-11:0900']['status'], status)

    def test_skipped_generation_is_bounded_to_two_attempts_and_preserves_cooldowns(self):
        runner = self.rolling_runner()
        runner.generate.return_value = {'text': '', 'skip': True}
        with patch('automation.post_queue.ROOT', Path(self.temp.name)):
            self.assertEqual(prepare_next_day_posts(runner), [])
        self.assertEqual(runner.generate.call_count, 2)
        self.assertEqual(set(runner.state['post_queue']['attempts']),
                         {'post:2026-10-11:0900', 'post:2026-10-11:1800'})
        self.assertFalse(runner.state['actions'])
        restarted = self.rolling_runner()
        with patch('automation.post_queue.ROOT', Path(self.temp.name)):
            self.assertEqual(prepare_next_day_posts(restarted),
                             ['post:2026-10-12:0900', 'post:2026-10-12:1800'])
        self.assertNotIn('2026-10-11', restarted.state['days'])
        self.assertEqual(restarted.generate.call_count, 2)

    def test_rolling_source_post_is_held_when_article_will_be_stale_on_publication_day(self):
        runner = self.rolling_runner()
        runner.policy['current_design_hours'] = [9]
        runner.state['actions'] = {
            f'post:{day}:{hour}00': {'kind': 'post', 'status': 'scheduled'}
            for day in ('2026-10-11', '2026-10-12') for hour in ('09', '18')
        }
        runner.state['design_news'] = {'checked_day': runner.day, 'items': [{
            'publisher': 'Linear', 'title': 'A design change',
            'url': 'https://linear.app/changelog/a-design-change',
            'published_at': (runner.now - timedelta(days=13)).isoformat(),
            'excerpt': 'An interface shows related work beside its current state.',
        }]}
        with patch('automation.post_queue.ROOT', Path(self.temp.name)), \
             patch('automation.design_news._fetch_feed') as read:
            self.assertEqual(prepare_next_day_posts(runner), ['post:2026-10-13:1800'])
        read.assert_not_called()
        self.assertEqual(runner.generate.call_count, 1)
        self.assertNotIn('post:2026-10-13:0900', runner.state['actions'])
        self.assertIn('post:2026-10-13:0900', runner.state['post_queue']['attempts'])

    def test_ordinary_live_run_refills_buffer_outside_engagement_hours_before_public_work(self):
        phases = []
        runner = Mock()
        runner.day = '2026-10-23'
        runner.local = datetime(2026, 10, 23, 12, tzinfo=ZONE)
        runner.now = runner.local.astimezone(timezone.utc)
        runner.counts = {'post': 0, 'interaction': 0, 'read': 0}
        runner.state = {'actions': {}}
        for name, label in (('post', 'post'), ('discover_published', 'published'),
                            ('replies', 'replies'), ('private_workflow', 'private'),
                            ('public_engagement', 'public'), ('save', 'save')):
            getattr(runner, name).side_effect = lambda *args, label=label, **kwargs: phases.append(label)
        with patch('automation.twin.Runner', return_value=runner), \
             patch('automation.twin.preflight') as verify, \
             patch('automation.twin.DiscoveryClient'), \
             patch('automation.post_queue.prepare_next_day_posts', side_effect=lambda value: phases.append('queue')) as prepare, \
             patch.dict(os.environ, {'TWIN_LIVE': 'true', 'TWIN_READ_ENABLED': 'true',
                                     'APIFY_TOKEN': 'test-placeholder'}, clear=True), \
             patch('sys.argv', ['twin', '--state', str(self.path)]), \
             patch('builtins.print'):
            twin.main()
        verify.assert_called_once()
        prepare.assert_called_once_with(runner)
        self.assertEqual(phases, ['post', 'published', 'queue', 'replies', 'private', 'public', 'save'])
        self.assertLess(runner.local.hour, 17)

    def test_main_model_queue_error_is_durable_sanitized_and_does_not_block_public_work(self):
        errors = (twin.ModelQualityError, twin.LocalModelError,
                  twin.apify_ai.ApifyModelError, twin.apify_ai.ApifyBudgetError)
        for error_type in errors:
            with self.subTest(error_type=error_type.__name__):
                runner = Mock(now=NOW, day='2026-10-10', counts={},
                              local=NOW.astimezone(ZONE), state={'actions': {}})
                order = []
                runner.save.side_effect = lambda: order.append('save')
                runner.public_engagement.side_effect = lambda *args: order.append('public')
                with patch('automation.twin.Runner', return_value=runner), \
                     patch('automation.twin.preflight'), patch('automation.twin.DiscoveryClient'), \
                     patch('automation.post_queue.prepare_next_day_posts',
                           side_effect=error_type('sensitive-provider-diagnostic')), \
                     patch.dict(os.environ, {'TWIN_LIVE': 'true', 'TWIN_READ_ENABLED': 'true',
                                             'APIFY_TOKEN': 'test-placeholder'}, clear=True), \
                     patch('sys.argv', ['twin']), patch('builtins.print') as log:
                    twin.main()
                status = runner.state['operational_status']['queue_preparation']
                self.assertEqual(status['status'], 'deferred')
                self.assertEqual(status['error_type'], error_type.__name__)
                self.assertEqual(order, ['save', 'public', 'save'])
                runner.replies.assert_called_once()
                self.assertNotIn('sensitive-provider-diagnostic', json.dumps(status))
                self.assertNotIn('sensitive-provider-diagnostic', str(log.call_args_list))

    def test_main_never_swallows_queue_write_or_durable_checkpoint_failures(self):
        for error_type in (twin.GitCheckpointError, twin.WriteCheckpointError, twin.WriteOutcomeError):
            with self.subTest(error_type=error_type.__name__):
                runner = Mock(now=NOW, day='2026-10-10', counts={}, state={'actions': {}})
                with patch('automation.twin.Runner', return_value=runner), \
                     patch('automation.twin.preflight'), \
                     patch('automation.post_queue.prepare_next_day_posts', side_effect=error_type('fixed failure')), \
                     patch.dict(os.environ, {'TWIN_LIVE': 'true'}, clear=True), \
                     patch('sys.argv', ['twin']), self.assertRaises(error_type):
                    twin.main()
                runner.public_engagement.assert_not_called()
                runner.replies.assert_not_called()

    def test_successful_queue_check_clears_old_model_error_without_claiming_full_coverage(self):
        runner = Mock(now=NOW, day='2026-10-10', counts={}, state={
            'actions': {}, 'operational_status': {'queue_preparation': {
                'status': 'deferred', 'error_type': 'ApifyModelError'}}})
        with patch('automation.twin.Runner', return_value=runner), \
             patch('automation.twin.preflight'), \
             patch('automation.post_queue.prepare_next_day_posts', return_value=[]), \
             patch.dict(os.environ, {'TWIN_LIVE': 'true'}, clear=True), \
             patch('sys.argv', ['twin']), patch('builtins.print'):
            twin.main()
        status = runner.state['operational_status']['queue_preparation']
        self.assertEqual(status['status'], 'checked')
        self.assertEqual(status['prepared_slots'], [])
        self.assertNotIn('error_type', status)

    def test_demo_and_model_preview_never_prepare_live_future_posts(self):
        for mode in ('--demo', '--preview'):
            runner = Mock()
            runner.day, runner.counts = '2026-10-23', {}
            runner.state = {'actions': {'preview': {'kind': 'post', 'status': 'dry-run'}}}
            with self.subTest(mode=mode), patch('automation.twin.Runner', return_value=runner), \
                 patch('automation.post_queue.prepare_next_day_posts') as prepare, \
                 patch.dict(os.environ, {'TWIN_LIVE': 'true'}, clear=True), \
                 patch('sys.argv', ['twin', mode]), patch('builtins.print'):
                twin.main()
            prepare.assert_not_called()
            runner.discover_published.assert_not_called()

    def test_prepares_tomorrow_at_nominal_times_when_no_morning_worker_wakes(self):
        runner = self.runner()
        self.assertEqual(prepare_next_day_posts(runner),
                         ['post:2026-10-11:0900', 'post:2026-10-11:1800'])
        actions = list(runner.state['actions'].values())
        self.assertEqual([action['scheduled_for'] for action in actions],
                         ['2026-10-11T03:30:00+00:00', '2026-10-11T12:30:00+00:00'])
        self.assertTrue(all(action['allocation_day'] == '2026-10-11' for action in actions))
        self.assertTrue(all(action['at'] == NOW.isoformat() for action in actions))
        self.assertEqual(runner.counts['post'], 0)
        self.assertEqual(runner.state['days']['2026-10-11']['post'], 2)
        self.assertEqual(runner.now, NOW)

    def test_restart_never_generates_or_resends_prepared_slots(self):
        prepare_next_day_posts(self.runner())
        restarted = self.runner()
        self.assertEqual(prepare_next_day_posts(restarted), [])
        restarted.generate.assert_not_called()
        self.assertEqual(len(restarted.state['actions']), 2)

    def test_future_legacy_morning_seed_reserves_only_morning(self):
        runner = self.runner()
        runner.state['actions']['post:2026-10-11'] = {
            'kind': 'post', 'status': 'scheduled', 'text': 'Existing morning post'}
        self.assertEqual(prepare_next_day_posts(runner), ['post:2026-10-11:1800'])
        self.assertEqual(runner.generate.call_count, 1)
        self.assertNotIn('post:2026-10-11:0900', runner.state['actions'])

    def test_unknown_and_rejected_claims_reserve_slots_even_when_counter_missing(self):
        for status in ('inflight', 'unknown-needs-reconciliation', 'rejected'):
            with self.subTest(status=status):
                runner = self.runner()
                runner.state['actions'] = {
                    'post:2026-10-11:0900': {'kind': 'post', 'status': status},
                    'other-seed': {'kind': 'post', 'status': status,
                                   'allocation_day': '2026-10-11'},
                }
                self.assertEqual(prepare_next_day_posts(runner), [])
                runner.generate.assert_not_called()

    def test_existing_future_counter_and_policy_limit_remain_authoritative(self):
        runner = self.runner()
        runner.state['days']['2026-10-11'] = {'post': 2}
        self.assertEqual(prepare_next_day_posts(runner), [])
        runner.generate.assert_not_called()
        runner.state['days']['2026-10-11']['post'] = 0
        runner.policy['max_posts_per_day'] = 1
        self.assertEqual(prepare_next_day_posts(runner), ['post:2026-10-11:0900'])

    def test_more_than_two_configured_hours_cannot_schedule_extra_posts(self):
        runner = self.runner()
        runner.policy.update(posting_hours=[9, 12, 18], max_posts_per_day=3)
        self.assertEqual(len(prepare_next_day_posts(runner)), 2)
        self.assertEqual(runner.generate.call_count, 2)

    def test_selects_tomorrows_plan_and_last_three_posts_without_fake_facts(self):
        runner = self.runner()
        runner.state['actions'] = {str(i): {'kind': 'post', 'text': str(i),
                                         'allocation_day': '2026-10-10'} for i in range(5)}
        prepare_next_day_posts(runner)
        first_task, context, supplied_policy = runner.generate.call_args_list[0].args
        self.assertEqual(context['content_brief']['date'], '2026-10-11')
        self.assertEqual(context['content_brief']['hour'], 9)
        self.assertEqual(context['source_notes'], [])
        self.assertEqual(context['background'], POLICY['background'])
        self.assertEqual(context['recent_posts'], ['2', '3', '4'])
        self.assertEqual(supplied_policy, runner.policy)
        for name in ('linkedin-content-planner', 'linkedin-post-writer', 'linkedin-humanizer'):
            self.assertIn('Skill: ' + name, first_task)

    def test_live_capacity_is_checked_before_drafting_and_again_before_remote_write(self):
        runner = self.runner(live=True)
        runner.policy['max_posts_per_day'] = 1
        response = Mock(status_code=200)
        response.json.return_value = {'postGroupId': 'tomorrow-morning'}
        with patch.object(runner, 'save'), \
             patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}), \
             patch('automation.twin.requests.post', return_value=response) as publish:
            prepare_next_day_posts(runner)
        self.assertEqual(runner.post_capacity.call_count, 2)
        self.assertEqual(publish.call_args.kwargs['json']['scheduledTime'], '2026-10-11T03:30:00Z')
        self.assertEqual(runner.state['actions']['post:2026-10-11:0900']['status'], 'scheduled')

    def test_full_remote_queue_does_not_generate_or_reserve_a_draft_attempt(self):
        runner = self.runner(live=True)
        runner.post_capacity.return_value = (False, 'Queue is full')
        with patch.object(runner, 'save') as save, patch('automation.twin.requests.post') as publish:
            self.assertEqual(prepare_next_day_posts(runner), [])
        runner.generate.assert_not_called()
        save.assert_not_called()
        publish.assert_not_called()
        self.assertNotIn('post_queue', runner.state)

    def test_capacity_change_after_draft_preserves_cooldown_without_remote_intent(self):
        runner = self.runner(live=True)
        runner.policy['max_posts_per_day'] = 1
        runner.post_capacity.side_effect = [(True, 'verified'), (False, 'Queue is full'),
                                            (False, 'Queue is full')]
        with patch.object(runner, 'save'), patch('automation.twin.requests.post') as publish:
            self.assertEqual(prepare_next_day_posts(runner), [])
        self.assertEqual(runner.generate.call_count, 1)
        publish.assert_not_called()
        self.assertFalse(runner.state['actions'])
        self.assertIn('post:2026-10-11:0900', runner.state['post_queue']['attempts'])

    def test_skipped_drafts_do_not_consume_quota_and_have_durable_two_hour_cooldown(self):
        runner = self.runner()
        runner.generate.return_value = {'skip': True}
        self.assertEqual(prepare_next_day_posts(runner), [])
        self.assertFalse(runner.state['actions'])
        self.assertNotIn('2026-10-11', runner.state['days'])
        restarted = self.runner()
        restarted.generate.return_value = {'skip': True}
        restarted.now += timedelta(hours=1, minutes=59)
        prepare_next_day_posts(restarted)
        restarted.generate.assert_not_called()
        restarted.now += timedelta(minutes=1)
        prepare_next_day_posts(restarted)
        self.assertEqual(restarted.generate.call_count, 2)

    def test_generation_error_checkpoints_attempt_before_model_call(self):
        runner = self.runner()
        def fail(*args):
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            self.assertIn('post:2026-10-11:0900', saved['post_queue']['attempts'])
            raise RuntimeError('provider unavailable')
        runner.generate.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, 'provider unavailable'):
            prepare_next_day_posts(runner)
        self.assertFalse(runner.state['actions'])
        restarted = self.runner()
        # The failed morning draft remains deferred; the independent evening
        # slot can still be prepared without retrying that model request.
        self.assertEqual(prepare_next_day_posts(restarted), ['post:2026-10-11:1800'])
        self.assertEqual(restarted.generate.call_count, 1)

    def test_remote_rejection_is_reserved_and_never_retried(self):
        runner = self.runner(live=True)
        runner.policy['max_posts_per_day'] = 1
        response = Mock(status_code=403)
        with patch.object(runner, 'save'), \
             patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-placeholder'}), \
             patch('automation.twin.requests.post', return_value=response) as publish:
            prepare_next_day_posts(runner)
            prepare_next_day_posts(runner)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(runner.generate.call_count, 1)
        self.assertEqual(runner.state['actions']['post:2026-10-11:0900']['status'], 'rejected')

    def test_excluded_weekday_paused_model_and_zero_quota_do_not_generate(self):
        for override in ({'posting_days': [0, 1, 2, 3, 4]}, {'max_posts_per_day': 0}):
            with self.subTest(override=override):
                runner = self.runner()
                runner.policy.update(override)
                self.assertEqual(prepare_next_day_posts(runner), [])
                runner.generate.assert_not_called()
        runner = self.runner()
        runner.model_paused = True
        self.assertEqual(prepare_next_day_posts(runner), [])
        runner.generate.assert_not_called()

    def test_uses_current_local_future_slots_when_utc_date_is_different(self):
        runner = self.runner()
        runner.now = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(prepare_next_day_posts(runner),
                         ['post:2026-10-11:0900', 'post:2026-10-11:1800'])

    def test_invalid_policy_and_naive_clock_fail_without_generation(self):
        for override in ({'posting_hours': [9, 9]}, {'posting_hours': [True]},
                         {'posting_days': [7]}, {'max_posts_per_day': -1},
                         {'posting_buffer_days': 0}, {'posting_buffer_days': 4},
                         {'posting_buffer_days': True}):
            with self.subTest(override=override):
                runner = self.runner()
                runner.policy.update(override)
                with self.assertRaises(ValueError):
                    prepare_next_day_posts(runner)
                runner.generate.assert_not_called()
        runner = self.runner()
        runner.now = NOW.replace(tzinfo=None)
        with self.assertRaises(ValueError):
            prepare_next_day_posts(runner)
        runner.generate.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class CheckpointCliExitTests(unittest.TestCase):
    def run_cli_failure(self, error):
        tree = ast.parse(Path(twin.__file__).read_text(encoding='utf-8'))
        guard = next(node for node in tree.body if isinstance(node, ast.If)
                     and isinstance(node.test, ast.Compare)
                     and isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__')
        namespace = {**vars(twin), '__name__': '__main__',
                     'main': Mock(side_effect=error), 'print': Mock()}
        code = compile(ast.Module(body=[guard], type_ignores=[]), twin.__file__, 'exec')
        with self.assertRaises(SystemExit) as stopped:
            exec(code, namespace)
        return stopped.exception.code, namespace['print']

    def test_durable_checkpoint_failures_have_a_distinct_supervisor_exit_code(self):
        for error_type in (twin.GitCheckpointError, twin.WriteCheckpointError, twin.PrivateWriteCheckpointError):
            with self.subTest(error_type=error_type.__name__):
                code, log = self.run_cli_failure(error_type('Fixed checkpoint diagnostic'))
                self.assertEqual(code, 78)
                self.assertIn('Fixed checkpoint diagnostic', str(log.call_args_list))

    def test_other_failures_keep_exit_one_without_exposing_provider_bodies(self):
        for error in (RuntimeError('private provider body'), twin.WriteOutcomeError('Fixed uncertain outcome')):
            with self.subTest(error_type=type(error).__name__):
                code, log = self.run_cli_failure(error)
                self.assertEqual(code, 1)
                self.assertNotIn('private provider body', str(log.call_args_list))
