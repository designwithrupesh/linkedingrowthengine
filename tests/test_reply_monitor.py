from datetime import datetime, timedelta, timezone
import json
import unittest

from automation.reply_monitor import (
    defer_reply, pending_replies, poll_targets, published_targets,
    queue_comments, record_poll,
)


NOW = datetime(2026, 10, 9, 9, 30, tzinfo=timezone.utc)
OWN = 'https://www.linkedin.com/in/therupeshkumar/'


def post(number=1):
    urn = 'urn:li:share:' + str(7500000000000000000 + number)
    return {'id': str(number), 'url': 'https://www.linkedin.com/feed/update/' + urn + '/',
            'post_urn': urn, 'text': 'A design trade-off.'}


def comment(number=1, author='someone', **fields):
    return {'comment_id': str(number), 'text': 'How do you evaluate that trade-off?',
            'author': {'name': author, 'profile_url': 'https://www.linkedin.com/in/' + author + '/'},
            **fields}


def state(posts=None):
    return {'posts': posts or [], 'actions': {}}


class ReplyMonitorTests(unittest.TestCase):
    def test_poll_rotation_survives_restart_and_one_failing_post(self):
        saved = state([post(1), post(2), post(3)])
        first = poll_targets(saved, NOW)[0]
        self.assertEqual(first['id'], '3')
        record_poll(saved, first, NOW, error=True)
        restarted = json.loads(json.dumps(saved))
        second = poll_targets(restarted, NOW + timedelta(minutes=5))[0]
        self.assertEqual(second['id'], '2')
        record_poll(restarted, second, NOW + timedelta(minutes=5), rows=[])
        self.assertEqual(poll_targets(restarted, NOW + timedelta(minutes=10))[0]['id'], '1')

    def test_one_post_does_not_poll_again_inside_configured_interval(self):
        saved = state([post()])
        record_poll(saved, post(), NOW, rows=[])
        self.assertEqual(poll_targets(saved, NOW + timedelta(seconds=299)), [])
        self.assertEqual(len(poll_targets(saved, NOW + timedelta(seconds=300))), 1)

    def test_latest_ten_valid_published_posts_and_canonical_identity(self):
        posts = [post(n) for n in range(12)]
        posts.append({'id': 'scheduled', 'url': None})
        posts.append({'id': 'malicious', 'url': 'https://example.com/urn:li:share:7500000000000000000/'})
        posts[-3]['url'] = 'https://www.linkedin.com/feed/update/urn:li:activity:7501000000000000000/'
        result = published_targets(state(posts))
        self.assertEqual(len(result), 10)
        self.assertEqual([item['id'] for item in result], [str(n) for n in reversed(range(2, 12))])
        self.assertEqual(result[0]['post_urn'], post(11)['post_urn'])

    def test_nested_replies_keep_top_parent_and_never_reply_to_owner(self):
        rows = [comment(1, author='therupeshkumar', replies=[
            comment(2, replies=[comment(3)]),
            comment(4, author='therupeshkumar'),
        ])]
        rows[0]['author']['profile_url'] = 'https://LINKEDIN.COM/in/TheRupeshKumar/?trk=feed#comment'
        saved = state([post()])
        stats = queue_comments(saved, post(), rows, OWN, NOW)
        pending = pending_replies(saved)
        self.assertEqual(stats['self'], 2)
        self.assertEqual([entry['comment']['id'] for entry in pending], ['2', '3'])
        self.assertEqual({entry['comment']['parent_id'] for entry in pending}, {'1'})
        self.assertEqual({entry['parent'] for entry in pending}, {'urn:li:comment:(' + post()['post_urn'] + ',1)'})
        self.assertTrue(all(entry['comment']['parent_text'] == rows[0]['text'] for entry in pending))

    def test_old_and_undated_comments_stay_pending_after_restart(self):
        saved = state([post()])
        queue_comments(saved, post(), [comment(1, posted_at={'timestamp': 1}), comment(2)], OWN, NOW)
        restarted = json.loads(json.dumps(saved))
        self.assertEqual(len(pending_replies(restarted, now=NOW + timedelta(days=10))), 2)

    def test_any_existing_write_intent_prevents_duplicate_including_unknown(self):
        for status in ('sent', 'inflight', 'unknown-needs-reconciliation', 'rejected', 'dry-run'):
            with self.subTest(status=status):
                saved = state([post()])
                queue_comments(saved, post(), [comment()], OWN, NOW)
                key = pending_replies(saved)[0]['key']
                saved['actions'][key] = {'kind': 'reply', 'status': status}
                self.assertEqual(pending_replies(saved), [])
                stats = queue_comments(saved, post(), [comment()], OWN, NOW)
                self.assertEqual(stats['queued'], 0)

    def test_generation_deferral_does_not_erase_reply_or_starve_next_comment(self):
        saved = state([post()])
        queue_comments(saved, post(), [comment(1), comment(2)], OWN, NOW)
        key = pending_replies(saved)[0]['key']
        defer_reply(saved, key, NOW, reason='private provider text')
        self.assertEqual([entry['comment']['id'] for entry in pending_replies(saved, now=NOW)], ['2'])
        self.assertEqual(len(pending_replies(saved, now=NOW + timedelta(minutes=15))), 2)
        self.assertEqual(saved['reply_monitor']['pending'][key]['deferred_reason'], 'generation-deferred')
        self.assertEqual(saved['actions'], {})

    def test_queue_overflow_and_actor_result_limits_are_explicit(self):
        saved = state([post()])
        stats = queue_comments(saved, post(), [comment(1), comment(2)], OWN, NOW, max_pending=1)
        self.assertEqual(stats['queued'], 1)
        self.assertEqual(stats['overflow'], 1)
        self.assertEqual(len(pending_replies(saved)), 1)
        coverage = record_poll(saved, post(), NOW, rows=[comment(1, totalComments=50)], max_items=100)
        self.assertEqual(coverage['coverage'], 'actor-result-limit')
        coverage = record_poll(saved, post(), NOW, rows=[comment(1)], max_items=1)
        self.assertEqual(coverage['coverage'], 'actor-result-limit')
        coverage = record_poll(saved, post(), NOW, rows=[], max_items=100)
        self.assertEqual(coverage['coverage'], 'bounded-public-results')

    def test_missing_author_identity_is_held_out_of_automatic_writes(self):
        saved = state([post()])
        unidentified = comment()
        unidentified['author'].pop('profile_url')
        stats = queue_comments(saved, post(), [unidentified], OWN, NOW)
        self.assertEqual(stats['unidentified'], 1)
        self.assertEqual(pending_replies(saved), [])

    def test_repeat_reads_update_edited_context_without_losing_queue_position(self):
        saved = state([post()])
        queue_comments(saved, post(), [comment()], OWN, NOW)
        entry = pending_replies(saved)[0]
        defer_reply(saved, entry['key'], NOW)
        queue_comments(saved, post(), [comment(text='An edited question.')], OWN, NOW + timedelta(minutes=1))
        edited = pending_replies(saved)[0]
        self.assertEqual(edited['comment']['text'], 'An edited question.')
        self.assertEqual(edited['first_seen_at'], NOW.isoformat())
        self.assertEqual(edited['generation_deferrals'], 1)

    def test_verified_clock_and_owner_are_required(self):
        with self.assertRaises(ValueError):
            poll_targets(state(), NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            queue_comments(state(), post(), [comment()], 'https://example.com/in/owner', NOW)


if __name__ == '__main__':
    unittest.main()
