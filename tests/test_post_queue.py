from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from automation.post_queue import prepare_next_day_posts
from automation.twin import Runner


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

    def test_uses_next_local_date_when_utc_date_is_different(self):
        runner = self.runner()
        runner.now = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(prepare_next_day_posts(runner),
                         ['post:2026-10-12:0900', 'post:2026-10-12:1800'])

    def test_invalid_policy_and_naive_clock_fail_without_generation(self):
        for override in ({'posting_hours': [9, 9]}, {'posting_hours': [True]},
                         {'posting_days': [7]}, {'max_posts_per_day': -1}):
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
