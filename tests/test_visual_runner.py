"""Visual publishing uses the runner's real durable quotas and source slots."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from automation.twin import Runner, WriteCheckpointError
from automation.visual_planning import prepare_visual
from automation.media_posts import MediaPostCheckpointError
from automation.post_queue import prepare_next_day_posts


ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / 'automation/policy.json').read_text(encoding='utf-8'))


class VisualRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.now = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
        self.runner = Runner(POLICY, self.path, now=self.now,
                             capacity=Mock(return_value=(True, 'verified')),
                             generate=Mock(side_effect=AssertionError('Reviewed visuals need no model')))
        self.asset = prepare_visual(self.runner,
            {'format': 'image', 'media_ref': 'product-point-of-view'},
            '2026-10-12', 18, Path(self.temp.name) / 'assets')
        self.schedule = datetime(2026, 10, 12, 12, 30, tzinfo=timezone.utc)

    def test_invalid_asset_is_refused_before_any_post_intent_or_quota(self):
        asset = dict(self.asset, sha256='0' * 64)
        with patch('automation.media_posts.publish_media_post') as publish:
            with self.assertRaises(ValueError):
                self.runner.write('post:2026-10-12:1800', 'post', self.asset['caption'],
                                  scheduled_time=self.schedule, media_asset=asset)
        self.assertEqual(self.runner.counts['post'], 0)
        self.assertEqual(self.runner.state['actions'], {})
        publish.assert_not_called()

    def test_live_media_starts_only_after_a_durable_quota_and_intent(self):
        self.runner.live = True
        events = []
        self.runner.save = lambda: events.append('checkpoint')
        def publish(runner, action, asset):
            self.assertEqual(events, ['checkpoint'])
            self.assertEqual(runner.counts['post'], 1)
            self.assertEqual(action['status'], 'inflight')
            self.assertEqual(action['format'], 'image')
            self.assertEqual(action['asset_spec']['title'], self.asset['spec']['title'])
            events.append('publish')
        with patch('automation.media_posts.publish_media_post', side_effect=publish) as remote:
            self.runner.write('post:2026-10-12:1800', 'post', self.asset['caption'],
                              scheduled_time=self.schedule, media_asset=self.asset)
            self.runner.write('post:2026-10-12:1800', 'post', self.asset['caption'],
                              scheduled_time=self.schedule, media_asset=self.asset)
        self.assertEqual(events, ['checkpoint', 'publish'])
        remote.assert_called_once()

    def test_media_checkpoint_failure_is_fatal_to_existing_session_contract(self):
        self.runner.live = True
        self.runner.save = Mock()
        with patch('automation.media_posts.publish_media_post', side_effect=MediaPostCheckpointError):
            with self.assertRaises(WriteCheckpointError):
                self.runner.write('post:2026-10-12:1800', 'post', self.asset['caption'],
                                  scheduled_time=self.schedule, media_asset=self.asset)

    def test_budget_paused_queue_can_still_prepare_a_reviewed_carousel(self):
        # Current nominal time has passed; this case isolates tomorrow's carousel.
        self.runner.now = self.schedule
        self.runner.policy = {**self.runner.policy, 'posting_buffer_days': 1}
        self.runner.model_paused = True
        result = prepare_next_day_posts(self.runner)
        self.assertEqual(result, ['post:2026-10-13:1800'])
        action = self.runner.state['actions'][result[0]]
        self.assertEqual(action['format'], 'carousel')
        self.assertEqual(action['scheduled_for'], '2026-10-13T12:30:00+00:00')
        self.assertEqual(self.runner.state['days']['2026-10-13']['post'], 1)
        self.runner.generate.assert_not_called()

    def test_declined_same_day_visual_has_a_durable_generation_cooldown(self):
        now = datetime(2026, 10, 12, 12, 30, tzinfo=timezone.utc)
        first = Runner(POLICY, self.path, now=now)
        with patch('automation.visual_planning.prepare_visual', return_value=None) as prepare:
            first.post()
            restarted = Runner(POLICY, self.path, now=now)
            restarted.post()
        prepare.assert_called_once()
        self.assertFalse(restarted.state['actions'])
        self.assertEqual(restarted.counts['post'], 0)
        self.assertIn('post:2026-10-12:1800', restarted.state['post_queue']['attempts'])


if __name__ == '__main__':
    unittest.main()
