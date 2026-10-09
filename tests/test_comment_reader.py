import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from automation.comment_reader import fetch_comments, group_comments


URN1 = 'urn:li:share:7500000000000000001'
URN2 = 'urn:li:ugcPost:7500000000000000002'


def post(urn=URN1, **fields):
    return {'post_urn': urn, 'url': 'https://www.linkedin.com/feed/update/' + urn + '/', **fields}


def comment(cid=1, **fields):
    return {'comment_id': str(cid), 'text': 'A useful question.', **fields}


class CommentReaderTests(unittest.TestCase):
    def test_one_actor_startup_per_batch_and_cache_bypassed(self):
        reader = Mock(POST_COMMENTS_ACTOR='known-actor')
        reader._run_sync.return_value = [comment(post_input=post()['url']), comment(2, post_input=URN2)]
        result = fetch_comments(reader, [post(), post(URN2)])
        reader._run_sync.assert_called_once_with('known-actor', {
            'postIds': [post()['url'], post(URN2)['url']], 'limit': 100, 'sortOrder': 'most relevant',
        }, force_refresh=True)
        self.assertEqual([row['comment_id'] for row in result['posts'][URN1]], ['1'])
        self.assertEqual([row['comment_id'] for row in result['posts'][URN2]], ['2'])

    def test_explicit_urn_input_and_documented_pagination(self):
        reader = Mock(POST_COMMENTS_ACTOR='known-actor')
        reader._run_sync.return_value = []
        fetch_comments(reader, [post()], max_items=20, page_number=2, input_format='urn')
        self.assertEqual(reader._run_sync.call_args.args[1], {
            'postIds': [URN1], 'limit': 20, 'sortOrder': 'most relevant', 'page_number': 2,
        })

    def test_exact_known_activity_read_url_can_map_to_distinct_canonical_share(self):
        activity = 'urn:li:activity:7501000000000000001'
        target = post(url='https://www.linkedin.com/feed/update/' + activity + '/')
        result = group_comments([comment(post_input=activity)], [target, post(URN2)])
        self.assertEqual(len(result['posts'][URN1]), 1)
        self.assertEqual(result['posts'][URN1][0]['post_input'], activity)

    def test_thread_read_uses_evidenced_activity_url_and_relevant_ordering(self):
        activity = 'urn:li:activity:7501000000000000001'
        read_url = 'https://www.linkedin.com/feed/update/' + activity + '/'
        reader = Mock(POST_COMMENTS_ACTOR='known-actor')
        reader._run_sync.return_value = [comment(1, post_input=read_url, replies=[
            comment(2, post_input=read_url, author={'profile_url': 'https://linkedin.com/in/therupeshkumar'}),
        ])]
        result = fetch_comments(reader, [post(read_url=read_url)])
        payload = reader._run_sync.call_args.args[1]
        self.assertEqual(payload['postIds'], [read_url])
        self.assertEqual(payload['sortOrder'], 'most relevant')
        self.assertEqual(result['posts'][URN1][0]['replies'][0]['comment_id'], '2')

    def test_activity_digits_are_never_relabelled_to_a_share(self):
        activity = 'urn:li:activity:' + URN1.rsplit(':', 1)[1]
        result = group_comments([comment(post_input=activity)], [post(), post(URN2)])
        self.assertEqual(result['posts'][URN1], [])
        self.assertEqual(result['unmapped'], 1)

    def test_numeric_post_input_matches_only_unambiguous_explicit_identifier(self):
        digits = URN1.rsplit(':', 1)[1]
        result = group_comments([comment(post_input=digits)], [post(), post(URN2)])
        self.assertEqual(len(result['posts'][URN1]), 1)
        conflicting = 'urn:li:ugcPost:' + digits
        result = group_comments([comment(post_input=digits)], [post(), post(conflicting)])
        self.assertEqual(result['conflicting'], 1)
        self.assertFalse(any(result['posts'].values()))

    def test_numeric_post_id_alone_cannot_select_one_post_in_a_batch(self):
        result = group_comments([comment(post_id=URN1.rsplit(':', 1)[1])], [post(), post(URN2)])
        self.assertEqual(result['unmapped'], 1)
        self.assertFalse(any(result['posts'].values()))

    def test_single_target_fallback_requires_no_usable_post_identity(self):
        result = group_comments([comment()], [post()])
        self.assertEqual(len(result['posts'][URN1]), 1)
        result = group_comments([comment(post_input=URN2)], [post()])
        self.assertEqual(result['posts'][URN1], [])
        self.assertEqual(result['unmapped'], 1)

    def test_missing_identity_in_multi_post_batch_is_held(self):
        result = group_comments([comment()], [post(), post(URN2)])
        self.assertEqual(result['unmapped'], 1)
        self.assertTrue(all(value['coverage'] == 'identity-unmapped' for value in result['coverage'].values()))

    def test_conflicting_post_input_and_comment_url_is_held(self):
        result = group_comments([comment(post_input=URN1, comment_url=post(URN2)['url'])], [post(), post(URN2)])
        self.assertEqual(result['conflicting'], 1)
        self.assertFalse(any(result['posts'].values()))

    def test_nested_cross_post_identity_is_filtered_before_any_reply(self):
        result = group_comments([comment(post_input=URN1, replies=[comment(2, post_input=URN2)])], [post(), post(URN2)])
        self.assertEqual(result['conflicting_nested'], 1)
        self.assertEqual(result['posts'][URN1][0]['replies'], [])

    def test_official_shape_flat_replies_resolve_top_parent_or_are_held(self):
        rows = [comment(1, post_input=URN1),
                comment(2, post_input=URN1, comment_type='reply', parent_comment_id='1'),
                comment(3, post_input=URN1, comment_type='reply', parent_comment_id='2'),
                comment(4, post_input=URN1, comment_type='reply', parent_comment_id='missing')]
        result = group_comments(rows, [post()])
        root = result['posts'][URN1][0]
        self.assertEqual(root['comment_id'], '1')
        self.assertEqual([row['comment_id'] for row in root['replies']], ['2', '3'])
        self.assertEqual(result['coverage'][URN1]['unresolved_replies'], 1)
        self.assertEqual(result['coverage'][URN1]['coverage'], 'thread-parent-missing')
        self.assertNotIn('replies', rows[0])

    def test_real_committed_fixture_maps_by_canonical_comment_url(self):
        rows = json.loads(Path('tests/fixtures/apify_comments.json').read_text(encoding='utf-8'))
        urn = 'urn:li:ugcPost:7320867198548279298'
        result = group_comments(rows, [post(urn), post(URN2)])
        self.assertEqual(len(result['posts'][urn]), 3)
        self.assertEqual(result['unmapped'], 0)
        self.assertEqual(result['coverage'][urn]['coverage'], 'actor-result-limit')
        self.assertEqual(result['coverage'][urn]['next_page'], 2)

    def test_summary_only_and_saturated_coverage_are_distinct(self):
        result = group_comments([{'summary': {'totalComments': 0}}], [post()])
        self.assertEqual(result['summaries'], 1)
        self.assertEqual(result['posts'][URN1], [])
        self.assertEqual(result['coverage'][URN1]['coverage'], 'bounded-public-results')
        result = group_comments([comment(post_input=URN1)], [post()], max_items=1)
        self.assertEqual(result['coverage'][URN1]['next_page'], 2)

    def test_invalid_batch_and_limits_fail_before_remote_call(self):
        reader = Mock()
        invalid = [([], {}), ([post()] * 6, {}), ([post(), post()], {}),
                   ([post()], {'max_items': 101}), ([post()], {'max_items': False}),
                   ([post()], {'page_number': 0}), ([post()], {'page_number': 101}),
                   ([post(url='https://example.com/' + URN1)], {})]
        for posts, options in invalid:
            with self.subTest(posts=posts, options=options):
                with self.assertRaises(ValueError):
                    fetch_comments(reader, posts, **options)
        reader._run_sync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
