"""A headline, unseen video, or course advert cannot supply a rebuttal."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from automation.engagement import candidate_targets, context_quality, stash_targets
from automation.twin import Runner


NOW = datetime(2026, 10, 18, 17, tzinfo=ZoneInfo('Asia/Calcutta'))
POLICY = {'timezone': 'Asia/Calcutta', 'profile_url': 'https://www.linkedin.com/in/owner/',
          'discovery_profiles': [], 'target_post_urls': [], 'react_to_target_posts': True,
          'max_public_interactions_per_day': 40, 'max_public_actions_per_session': 2}


def post(text, index=0):
    urn = 'urn:li:share:' + str(7000000000000000000 + index)
    return {'urn': urn, 'url': 'https://www.linkedin.com/feed/update/' + urn + '/',
            'text': text, 'authorProfileUrl': 'https://www.linkedin.com/in/designer/'}


class ContextQualityTests(unittest.TestCase):
    def test_recorded_title_and_video_captions_do_not_supply_the_missing_argument(self):
        for text in ('The only four real moats\n\nFull post: https://lnkd.in/gm3Dx__e',
                     'Head of Design at Stripe, Katie Dill, on how to stop building zombie UIs',
                     'Head of Design at Stripe, Katie Dill, on how to stop building zombie UIs.',
                     "Katie Dill's talk from the Lenny & Friends Summit is now my third most viewed YT video of all-time.\nHere's the talk: https://lnkd.in/gcec6qjp",
                     'Best $ I\u2019ve spent in years', 'Design is important.',
                     'Watch the full episode: https://lnkd.in/example'):
            with self.subTest(text=text):
                self.assertFalse(context_quality(post(text))[0])

    def test_missing_or_fragmentary_context_is_rejected(self):
        for text in (None, '', '   ', 'https://example.com/article', 'New navigation design',
                     'The design decision every early-stage team should think about.',
                     'A specific product-design trade-off to discuss.'):
            with self.subTest(text=text):
                self.assertFalse(context_quality(post(text))[0])

    def test_concise_complete_design_assertions_and_real_questions_remain_eligible(self):
        for text in ('A visible undo button is better than a confirmation dialog.',
                     'Keep error messages beside the input.',
                     'Typography needs room to breathe.',
                     'How should an early product team decide what to simplify first?',
                     'Hiding prices until checkout makes comparison harder.',
                     'The spacing around that heading makes it feel calmer.',
                     'A persistent undo action is better than another confirmation dialog. https://example.com/example'):
            with self.subTest(text=text):
                self.assertEqual(context_quality(post(text)), (True, 'self-contained-context'))

    def test_short_linked_excerpts_do_not_become_the_full_article(self):
        text = ('Switching costs make a product harder to leave, but they can come from awkward export '
                'tools as well as an essential workflow. Full post: https://example.com/article')
        self.assertEqual(context_quality(post(text)), (False, 'external-teaser'))

    def test_substantial_self_contained_source_can_also_link_a_full_article(self):
        text = ('A customer should be able to export their work without opening a support ticket. '
                'An awkward export tool can create switching costs, but that makes the product harder to leave '
                'rather than better to use. I would rather make the everyday workflow worth returning to. '
                'Full post: https://example.com/article')
        self.assertEqual(context_quality(post(text)), (True, 'self-contained-context'))

    def test_actual_enrollment_ticket_and_sponsored_promotions_are_excluded(self):
        cases = (
            'Is a 6-hour Saturday workshop worth it? Look at who signs up. Next cohort is this Saturday. https://example.com/course',
            'Quick note: my final Product Taste cohort of 2026 is this Saturday and enrollment closes in 2 days.',
            'Final reminder about Product Taste. Here is what past students are saying: https://example.com/reviews',
            'Join us for our first Linear conference on continuing the craft of great software.',
            'Tickets now available for the design conference. https://example.com/tickets',
            '#ad A visible undo button is better than a confirmation dialog.',
            'Paid partnership: our new design tool is ready for product teams.',
            'This video is sponsored by a tool for product teams. It compares different label sizes.',
            'Thank you to our wonderful sponsors WorkOS and DX for supporting this podcast.',
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(context_quality(post(text)), (False, 'promotion'))

    def test_educational_discussion_and_actual_product_changes_are_not_ads(self):
        for text in ('A design workshop needs a clear decision for participants to make. Otherwise the team leaves with notes and no next step.',
                     'Course exercises should give students room to disagree about a layout. A single prescribed answer hides the judgement involved.',
                     'You can now swipe any message to reply or forward. You do not even have to expand the message first.'):
            with self.subTest(text=text):
                self.assertTrue(context_quality(post(text))[0])

    def test_cached_decision_survives_restart_without_action_or_quota_mutations(self):
        state = {'actions': {}, 'days': {'2026-10-18': {'public_interaction': 0}}}
        teaser = post('The only four real moats\nFull post: https://lnkd.in/example')
        stash_targets(NOW, POLICY, state, [teaser])
        self.assertEqual(candidate_targets(NOW, POLICY, state), [])
        self.assertEqual(state['engagement']['targets'][0]['quality_decision'],
                         {'eligible': False, 'reason': 'external-teaser'})
        persisted = json.loads(json.dumps(state))
        before = json.dumps(persisted, sort_keys=True)
        self.assertEqual(candidate_targets(NOW, POLICY, persisted), [])
        self.assertEqual(json.dumps(persisted, sort_keys=True), before)
        self.assertFalse(persisted['actions'])
        self.assertEqual(persisted['days']['2026-10-18']['public_interaction'], 0)

    def test_refreshed_full_context_can_become_eligible_without_changing_identity(self):
        state = {}
        teaser = post('The only four real moats\nFull post: https://lnkd.in/example')
        stash_targets(NOW, POLICY, state, [teaser])
        self.assertFalse(candidate_targets(NOW, POLICY, state))
        complete = dict(teaser, text='A visible undo button is better than a confirmation dialog.')
        stash_targets(NOW, POLICY, state, [complete])
        self.assertEqual(len(candidate_targets(NOW, POLICY, state)), 1)
        self.assertEqual(len(state['engagement']['targets']), 1)

    def test_runner_skips_cached_teaser_before_model_actor_or_public_write(self):
        with tempfile.TemporaryDirectory() as temp:
            generate = Mock(side_effect=AssertionError('A teaser must not reach generation'))
            runner = Runner(POLICY, Path(temp) / 'state.json', now=NOW, generate=generate)
            stash_targets(NOW, POLICY, runner.state,
                          [post('The only four real moats\nFull post: https://lnkd.in/example')])
            reader = Mock()
            with patch('automation.twin.requests.post') as write:
                runner.public_engagement(reader)
            generate.assert_not_called()
            reader.fetch_post.assert_not_called()
            reader.fetch_profile_posts.assert_not_called()
            write.assert_not_called()
            self.assertFalse(runner.state['actions'])


if __name__ == '__main__':
    unittest.main()
