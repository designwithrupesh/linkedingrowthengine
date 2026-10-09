"""Runner integration contracts for the owner's expanded growth policy.

Actors, model inference and LinkedIn writes are replaced with deterministic
fixtures. Durable state remains real so restart and queue assertions exercise
the runner's persistence rather than a mock of its implementation.
"""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from automation import apify_ai, engagement, reply_monitor
from automation.comment_signal import CommentSignals
from automation.twin import Runner
from lib.url_parser import build_parent_comment_urn


POLICY = json.loads(Path('automation/policy.json').read_text(encoding='utf-8'))
ZONE = ZoneInfo('Asia/Calcutta')
TEXT = 'A clear customer task gives a product team a useful basis for deciding what to simplify.'
OWN_URN = 'urn:li:share:7500000000000000001'
PUBLIC_URN = 'urn:li:ugcPost:7500000000000000002'
PUBLIC_ACTIVITY = 'urn:li:activity:7500000000000000003'


def clock(hour=9, minute=0, day=10):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE).astimezone(timezone.utc)


def owned_post(urn=OWN_URN):
    return {'id': 'owned-post', 'urn': urn, 'post_urn': urn,
            'url': 'https://www.linkedin.com/feed/update/' + urn + '/',
            'text': 'A useful early product starts with a clear customer task.'}


def public_post():
    return {'urn': PUBLIC_URN, 'shareUrn': PUBLIC_URN,
            'url': 'https://www.linkedin.com/feed/update/' + PUBLIC_ACTIVITY + '/',
            'authorName': 'A founder', 'authorProfileUrl': 'https://www.linkedin.com/in/founder/',
            'text': 'How should an early product team decide what to simplify first?',
            'postedAtISO': clock(8).isoformat()}


def comments(count=1):
    return [{'comment_id': str(100 + index), 'post_input': OWN_URN,
             'text': 'How should we choose the first customer task to test?',
             'author': {'name': 'Product leader', 'profile_url': 'https://www.linkedin.com/in/productleader/'}}
            for index in range(count)]


class ExpandedRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.json'
        self.send = self.enterContext(patch('automation.twin.requests.post'))
        self.send.return_value = Mock(status_code=201)
        self.send.return_value.json.return_value = {'success': True, 'comment': {'id': 'remote-comment'}}
        self.enterContext(patch('automation.twin.requests.get'))

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, now=None, **kwargs):
        policy = copy.deepcopy(POLICY)
        policy.update(discovery_profiles=[], target_post_urls=[])
        return Runner(policy, self.path, now=now or clock(),
                      generate=kwargs.pop('generate', Mock(return_value={'text': TEXT})),
                      capacity=lambda *args: (True, 'verified capacity'), **kwargs)

    def reader(self, rows=None):
        reader = Mock(POST_COMMENTS_ACTOR='fixture-comments-actor')
        reader._run_sync.return_value = rows or []
        reader.fetch_post_comments.return_value = rows or []
        return reader

    def test_weekend_morning_and_evening_slots_survive_restarts(self):
        first = self.runner()
        first.post()
        repeated = self.runner(now=clock(9, 15))
        repeated.post()
        repeated.generate.assert_not_called()
        evening = self.runner(now=clock(18))
        evening.post()
        self.assertEqual(set(evening.state['actions']),
                         {'post:2026-10-10:0900', 'post:2026-10-10:1800'})
        self.assertEqual(evening.counts['post'], 2)
        self.assertEqual(evening.state['actions']['post:2026-10-10:0900']['scheduled_for'],
                         '2026-10-10T03:35:00+00:00')
        self.assertEqual(evening.state['actions']['post:2026-10-10:1800']['scheduled_for'],
                         '2026-10-10T12:35:00+00:00')
        self.send.assert_not_called()

    def test_existing_seed_reserves_morning_without_losing_evening_slot(self):
        morning = self.runner(now=clock(day=13))
        morning.state['actions']['post:2026-10-13'] = {
            'kind': 'post', 'status': 'scheduled', 'text': TEXT,
            'at': clock(day=9).isoformat(), 'allocation_day': '2026-10-13',
            'scheduled_for': '2026-10-13T03:30:00+00:00',
        }
        morning.counts['post'] = 1
        morning.save()
        morning.post()
        morning.generate.assert_not_called()
        evening = self.runner(now=clock(18, day=13))
        evening.post()
        self.assertEqual(set(evening.state['actions']),
                         {'post:2026-10-13', 'post:2026-10-13:1800'})
        self.assertEqual(evening.counts['post'], 2)

    def test_slow_draft_keeps_slot_identity_and_refreshes_publication_lead(self):
        with patch('automation.twin.monotonic', side_effect=[100, 461]):
            runner = self.runner()
            runner.post()
        action = runner.state['actions']['post:2026-10-10:0900']
        self.assertEqual(action['scheduled_for'], '2026-10-10T03:41:01+00:00')
        self.assertEqual(action['allocation_day'], '2026-10-10')
        restarted = self.runner(now=clock(9, 15))
        restarted.post()
        restarted.generate.assert_not_called()
        self.assertEqual(restarted.counts['post'], 1)

    def test_slow_draft_cannot_move_a_slot_into_the_next_local_date(self):
        with patch('automation.twin.monotonic', side_effect=[100, 520]):
            runner = self.runner(now=clock(23, 54))
            runner.policy['posting_hours'] = [23]
            runner.post()
        runner.generate.assert_called_once()
        self.assertFalse(runner.state['actions'])
        self.assertEqual(runner.counts['post'], 0)
        self.send.assert_not_called()

    def test_weekly_analytics_remains_retryable_when_read_or_draft_is_deferred(self):
        runner = self.runner(generate=Mock(return_value={'skip': True}))
        runner.state['posts'].append(owned_post())
        reader = self.reader()
        reader.fetch_post_engagers.return_value = []
        runner.analytics(reader)
        self.assertFalse(runner.state['reports'])
        runner.generate.return_value = {'text': TEXT}
        with patch.object(runner, 'read', return_value=None):
            runner.analytics(reader)
        self.assertFalse(runner.state['reports'])
        runner.analytics(reader)
        self.assertEqual(len(runner.state['reports']), 1)
        self.assertEqual(runner.state['reports'][0]['summary'], TEXT)

    def test_sixty_owned_posts_limit_unavailable_statistics_to_five_per_run(self):
        runner = self.runner()
        runner.state['posts'] = [owned_post(f'urn:li:share:{7500000000000000001 + index}')
                                 for index in range(60)]
        transport = Mock()
        transport.get.return_value = Mock(status_code=200)
        transport.get.return_value.json.return_value = {'success': True, 'context': {
            'features': {'apiAccess': True, 'analytics': True}}}
        transport.post.return_value = Mock(status_code=429)
        runner.comment_signals = CommentSignals('linkedin-owner', 'fixture-token', transport)
        reader = self.reader()
        runner.replies(reader)
        self.assertEqual(transport.post.call_count, 5)
        first = set(runner.state['reply_monitor']['posts'])
        self.assertEqual(len(first), 5)
        restarted = self.runner(now=clock(9, 5))
        restarted.comment_signals = CommentSignals('linkedin-owner', 'fixture-token', transport)
        restarted.replies(reader)
        self.assertEqual(transport.post.call_count, 10)
        self.assertEqual(len(restarted.state['reply_monitor']['posts']), 10)
        self.assertEqual(reader._run_sync.call_count, 2)
        restarted.generate.assert_not_called()

    def test_exact_date_and_hour_select_the_planned_consulting_brief(self):
        runner = self.runner(now=clock(18, day=13))
        runner.post()
        context = runner.generate.call_args.args[1]
        plan = json.loads(Path('automation/content-plan.json').read_text(encoding='utf-8'))
        expected = next(item for item in plan['slots']
                        if item['date'] == '2026-10-13' and item['hour'] == 18)
        self.assertEqual(context['content_brief'], expected)
        self.assertEqual(context['topic'], expected['topic'])
        self.assertEqual(context['source_notes'], [])
        self.assertIn('linkedin-post-writer', runner.generate.call_args.args[0])

    def test_public_forty_and_reply_thousand_have_independent_durable_limits(self):
        runner = self.runner()
        self.assertEqual(runner.policy['max_public_interactions_per_day'], 40)
        self.assertEqual(runner.policy['max_replies_per_day'], 1000)
        with patch.object(runner, 'save'):
            for index in range(40):
                kind = 'reaction' if index % 2 else 'comment'
                runner.write('public:' + str(index), kind, 'LIKE' if kind == 'reaction' else TEXT,
                             PUBLIC_URN)
            runner.write('public:over-limit', 'comment', TEXT, PUBLIC_URN)
            runner.counts['reply'] = 999
            runner.write('reply:last', 'reply', TEXT, OWN_URN, 'parent-comment-urn')
            runner.write('reply:over-limit', 'reply', TEXT, OWN_URN, 'parent-comment-urn')
        runner.save()
        restarted = self.runner()
        self.assertEqual(restarted.counts['public_interaction'], 40)
        self.assertEqual(restarted.counts['interaction'], 40)
        self.assertEqual(restarted.counts['reply'], 1000)
        self.assertIn('reply:last', restarted.state['actions'])
        self.assertNotIn('public:over-limit', restarted.state['actions'])
        self.assertNotIn('reply:over-limit', restarted.state['actions'])

    def test_queued_reply_runs_before_cached_public_comment_and_both_deduplicate(self):
        runner = self.runner(now=clock(17))
        own = owned_post()
        runner.state['posts'].append(own)
        reply_monitor.queue_comments(runner.state, own, comments(), runner.policy['profile_url'], runner.now)
        engagement.stash_targets(runner.now, runner.policy, runner.state, [public_post()])
        reader = self.reader(comments())
        runner.interactions(reader)
        kinds = ['reply' if 'comment' in call.args[1] else 'comment'
                 for call in runner.generate.call_args_list]
        self.assertEqual(kinds, ['reply', 'comment'])
        self.assertEqual([action['kind'] for action in runner.state['actions'].values()],
                         ['reply', 'reaction', 'comment'])
        reader.fetch_post.assert_not_called()
        runner.save()
        restarted = self.runner(now=clock(17, 5))
        restarted.interactions(self.reader(comments()))
        self.assertEqual(len(restarted.state['actions']), 3)
        restarted.generate.assert_not_called()
        self.assertEqual(reply_monitor.pending_replies(restarted.state), [])

    def test_unknown_reaction_quarantines_explicit_target_before_actor_read(self):
        runner = self.runner(now=clock(17))
        post = public_post()
        runner.policy['target_post_urls'] = [post['url']]
        legacy = 'comment:' + hashlib.sha256(post['url'].encode()).hexdigest()
        runner.state['actions']['reaction:' + legacy] = {
            'kind': 'reaction', 'status': 'unknown-needs-reconciliation', 'post_urn': PUBLIC_ACTIVITY,
        }
        reader = self.reader()
        runner.public_engagement(reader)
        reader.fetch_post.assert_not_called()
        reader._run_sync.assert_not_called()
        runner.generate.assert_not_called()
        self.send.assert_not_called()

    def test_shared_ai_budget_pause_preserves_pending_reply_and_prevents_all_writes(self):
        generator = Mock(side_effect=apify_ai.ApifyBudgetError('configured budget unavailable'))
        runner = self.runner(generate=generator, live=True)
        own = owned_post()
        runner.state['posts'].append(own)
        reply_monitor.queue_comments(runner.state, own, comments(), runner.policy['profile_url'], runner.now)
        engagement.stash_targets(runner.now, runner.policy, runner.state, [public_post()])
        reader = self.reader()
        with patch.object(runner, 'save'):
            runner.interactions(reader)
            runner.post()
        self.assertTrue(runner.model_paused)
        self.assertEqual(runner.state['operational_status']['ai'], 'budget-paused')
        self.assertEqual(len(reply_monitor.pending_replies(runner.state)), 1)
        self.assertFalse(runner.state['actions'])
        generator.assert_called_once()
        reader._run_sync.assert_not_called()
        reader.fetch_post.assert_not_called()
        self.send.assert_not_called()

    def test_unconnected_inbox_does_not_block_existing_public_connection(self):
        runner = self.runner(now=clock(17), live=True)
        runner.policy['dm_replies_enabled'] = True
        engagement.stash_targets(runner.now, runner.policy, runner.state, [public_post()])
        with patch('automation.twin.UnipileClient.from_environment', return_value=None), \
             patch.object(runner, 'save'), \
             patch.dict(os.environ, {'PUBLORA_API_KEY': 'publora-test-placeholder'}, clear=True):
            runner.private_workflow()
            runner.public_engagement(self.reader())
        self.assertEqual(runner.state['operational_status']['private_connection'], 'not-connected')
        self.assertEqual([item['kind'] for item in runner.state['actions'].values()], ['reaction', 'comment'])
        self.assertTrue(all(item['status'] == 'sent' for item in runner.state['actions'].values()))
        self.assertEqual(self.send.call_count, 2)

    def test_all_observed_comments_survive_model_skip_and_restart_reply_cap(self):
        runner = self.runner(generate=Mock(return_value={'skip': True}))
        own = owned_post()
        runner.state['posts'].append(own)
        reader = self.reader(comments(30))
        runner.replies(reader)
        runner.save()
        expected_keys = {'reply:' + OWN_URN + ':' + str(100 + index) for index in range(30)}
        self.assertEqual(set(runner.state['reply_monitor']['pending']), expected_keys)
        self.assertFalse(runner.state['actions'])
        restarted = self.runner(now=clock(9, 16))
        self.assertEqual(set(restarted.state['reply_monitor']['pending']), expected_keys)
        restarted.replies(self.reader(comments(30)))
        restarted.save()
        self.assertEqual(restarted.counts['reply'], 20)
        self.assertEqual(len(restarted.state['actions']), 20)
        self.assertEqual(len(reply_monitor.pending_replies(restarted.state)), 10)
        final = self.runner(now=clock(9, 22))
        final.replies(self.reader(comments(30)))
        self.assertEqual(final.counts['reply'], 30)
        self.assertEqual(set(final.state['actions']), expected_keys)
        self.assertEqual(reply_monitor.pending_replies(final.state), [])
        self.send.assert_not_called()

    def test_one_fresh_batch_preserves_each_posts_canonical_reply_parent(self):
        runner = self.runner()
        second_urn = 'urn:li:ugcPost:7500000000000000004'
        first, second = owned_post(), owned_post(second_urn)
        runner.state['posts'].extend([first, second])
        rows = comments() + [
            {**comments()[0], 'comment_id': '201', 'post_input': second_urn,
             'text': 'Which decision should the designer own?'},
            {**comments()[0], 'comment_id': '202', 'post_input': second_urn,
             'text': 'How would that change for a small team?',
             'comment_type': 'reply', 'parent_comment_id': '201'},
        ]
        reader = self.reader(rows)
        runner.replies(reader)
        reader._run_sync.assert_called_once()
        reader.fetch_post_comments.assert_not_called()
        self.assertEqual(set(reader._run_sync.call_args.args[1]['postIds']),
                         {first['url'], second['url']})
        self.assertTrue(reader._run_sync.call_args.kwargs['force_refresh'])
        self.assertEqual(runner.counts['read'], 1)
        self.assertEqual(runner.counts['reply'], 3)
        nested = runner.state['actions']['reply:' + second_urn + ':202']
        self.assertEqual(nested['post_urn'], second_urn)
        self.assertEqual(nested['parent'], build_parent_comment_urn(second_urn, '201'))
        contexts = [call.args[1] for call in runner.generate.call_args_list]
        nested_context = next(item for item in contexts if item['comment']['id'] == '202')
        self.assertEqual(nested_context['comment']['parent_text'], 'Which decision should the designer own?')

    def test_saturated_comment_page_checkpoint_advances_after_restart(self):
        runner = self.runner()
        runner.policy['max_replies_per_run'] = 0
        runner.state['posts'].append(owned_post())
        rows = [{**row, 'totalComments': 200} for row in comments(100)]
        reader = self.reader(rows)
        runner.replies(reader)
        runner.save()
        self.assertEqual(len(reply_monitor.pending_replies(runner.state)), 100)
        self.assertEqual(reader._run_sync.call_args.args[1].get('page_number', 1), 1)
        restarted = self.runner(now=clock(9, 5))
        restarted.policy['max_replies_per_run'] = 0
        # A cached, unchanged statistic must not block the next actor page.
        transport = Mock()
        restarted.comment_signals = CommentSignals('linkedin-owner', 'fixture-token', transport)
        restarted.state['comment_signals'] = {'posts': {OWN_URN: {
            'count': 200, 'status': 'analytics-count', 'changed_at': runner.now.isoformat(),
            'last_checked_at': runner.now.isoformat()}}}
        next_rows = [{**row, 'comment_id': str(300 + index), 'totalComments': 200}
                     for index, row in enumerate(comments(100))]
        next_reader = self.reader(next_rows)
        restarted.replies(next_reader)
        next_reader._run_sync.assert_called_once()
        self.assertEqual(next_reader._run_sync.call_args.args[1]['page_number'], 2)
        self.assertEqual(len(reply_monitor.pending_replies(restarted.state)), 200)
        transport.get.assert_not_called()
        transport.post.assert_not_called()
        restarted.generate.assert_not_called()
        self.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()

class InteractionVoiceTests(unittest.TestCase):
    def test_generic_praise_is_rejected_without_damaging_specific_text(self):
        from automation.twin import validate_model_output
        generic='A strong reminder that speed does not equal quality. '+('A concrete design observation about customer decisions. '*4)
        specific='Keeping the next action visible reduces the need for a tooltip. '+('A concrete design observation about customer decisions. '*3)
        self.assertTrue(validate_model_output({'text':generic,'skip':False},'comment')['skip'])
        result=validate_model_output({'text':specific,'skip':False},'comment')
        self.assertFalse(result['skip'])
        self.assertEqual(result['text'],specific.strip())
