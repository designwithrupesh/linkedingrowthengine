from datetime import datetime, timezone
import unittest
from zoneinfo import ZoneInfo

from automation.cadence import claimed_post_count, due_slots, slot_key


ZONE = ZoneInfo('Asia/Calcutta')
POLICY = {'timezone': 'Asia/Calcutta', 'posting_hours': [9, 18],
          'posting_days': list(range(7)), 'max_posts_per_day': 2}


def at(hour, minute=0, **kwargs):
    return datetime(2026, 10, 10, hour, minute, tzinfo=ZONE, **kwargs)


def state(actions=None, count=0):
    return {'actions': actions or {}, 'days': {'2026-10-10': {'post': count}}}


class CadenceTests(unittest.TestCase):
    def test_canonical_slot_key(self):
        self.assertEqual(slot_key('2026-10-10', 9), 'post:2026-10-10:0900')
        self.assertEqual(slot_key('2026-10-10', 18, 5), 'post:2026-10-10:1805')

    def test_default_has_both_daily_slots_on_weekends(self):
        morning = due_slots(at(9), {}, state())
        evening = due_slots(at(18), {}, state())
        self.assertEqual([slot.key for slot in morning], ['post:2026-10-10:0900'])
        self.assertEqual([slot.key for slot in evening], ['post:2026-10-10:1800'])

    def test_exact_slot_publishes_five_minutes_in_future(self):
        slot = due_slots(at(9), POLICY, state())[0]
        self.assertEqual(slot.nominal_time, at(9).astimezone(timezone.utc))
        self.assertEqual(slot.scheduled_time, at(9, 5).astimezone(timezone.utc))
        self.assertEqual(slot.allocation_day, '2026-10-10')

    def test_pre_slot_does_not_publish_or_advance_evening(self):
        self.assertEqual(due_slots(at(8, 59), POLICY, state()), [])
        self.assertEqual(due_slots(at(17, 59), POLICY, state()), [])

    def test_delay_keeps_nominal_identity_and_respects_exact_boundary(self):
        slot = due_slots(at(11), POLICY, state())[0]
        self.assertEqual(slot.key, 'post:2026-10-10:0900')
        self.assertEqual(slot.scheduled_time, at(11, 5).astimezone(timezone.utc))
        self.assertEqual(due_slots(at(11, microsecond=1), POLICY, state()), [])

    def test_missed_morning_is_not_backfilled_by_evening(self):
        self.assertEqual(due_slots(at(14), POLICY, state()), [])
        self.assertEqual([slot.key for slot in due_slots(at(18), POLICY, state())],
                         ['post:2026-10-10:1800'])

    def test_utc_clock_uses_indian_calendar_and_slot(self):
        slot = due_slots(datetime(2026, 10, 10, 3, 30, tzinfo=timezone.utc), POLICY, state())[0]
        self.assertEqual(slot.allocation_day, '2026-10-10')
        self.assertEqual(slot.key, 'post:2026-10-10:0900')

    def test_new_day_never_claims_yesterdays_missed_slot(self):
        self.assertEqual(due_slots(datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc), POLICY, state()), [])

    def test_publication_cannot_roll_into_next_day(self):
        late_policy = {**POLICY, 'posting_hours': [23]}
        self.assertEqual(due_slots(at(23, 56), late_policy, state()), [])

    def test_legacy_seed_claims_morning_and_counts_for_evening(self):
        seeded = state({'post:2026-10-10': {'kind': 'post', 'status': 'scheduled'}}, 1)
        self.assertEqual(due_slots(at(9), POLICY, seeded), [])
        evening = due_slots(at(18), POLICY, seeded)
        self.assertEqual([slot.key for slot in evening], ['post:2026-10-10:1800'])
        self.assertEqual(claimed_post_count('2026-10-10', seeded), 1)

    def test_unknown_canonical_intent_never_retries(self):
        claimed = state({'post:2026-10-10:1800': {'kind': 'post', 'status': 'unknown-needs-reconciliation'}})
        self.assertEqual(due_slots(at(18), POLICY, claimed), [])

    def test_dry_run_and_rejected_intents_reserve_daily_quota(self):
        claimed = state({
            'post:2026-10-10': {'kind': 'post', 'status': 'dry-run'},
            'post:2026-10-10:1800': {'kind': 'post', 'status': 'rejected'},
        })
        self.assertEqual(claimed_post_count('2026-10-10', claimed), 2)
        self.assertEqual(due_slots(at(9), POLICY, claimed), [])

    def test_stale_counter_does_not_remove_existing_seed_from_daily_quota(self):
        claimed = state({
            'post:2026-10-10': {'kind': 'post', 'status': 'scheduled'},
            'prepared-other': {'kind': 'post', 'allocation_day': '2026-10-10', 'status': 'inflight'},
        })
        self.assertEqual(claimed_post_count('2026-10-10', claimed), 2)
        self.assertEqual(due_slots(at(18), POLICY, claimed), [])

    def test_persisted_counter_remains_authoritative_when_receipts_missing(self):
        self.assertEqual(due_slots(at(18), POLICY, state(count=2)), [])

    def test_future_seed_is_allocated_to_its_publication_day(self):
        seeded = state({'future': {'kind': 'post', 'at': at(9).isoformat(),
                                 'scheduled_for': '2026-10-11T03:30:00+00:00'}}, 0)
        self.assertEqual(claimed_post_count('2026-10-10', seeded), 0)
        self.assertEqual(claimed_post_count('2026-10-11', seeded), 1)

    def test_seed_on_other_day_does_not_claim_todays_slot(self):
        seeded = state({'post:2026-10-11': {'kind': 'post', 'status': 'scheduled'}})
        self.assertEqual(len(due_slots(at(9), POLICY, seeded)), 1)

    def test_explicit_weekday_policy_is_respected(self):
        self.assertEqual(due_slots(at(9), {**POLICY, 'posting_days': [0, 1, 2, 3, 4]}, state()), [])

    def test_new_hours_override_legacy_single_hour(self):
        self.assertEqual(len(due_slots(at(18), {**POLICY, 'posting_hour': 9}, state())), 1)
        self.assertEqual(due_slots(at(18), {'posting_hour': 9}, state()), [])

    def test_unknown_non_post_action_does_not_consume_post_quota(self):
        reacted = state({'reaction:a': {'kind': 'reaction', 'at': at(9).isoformat(),
                                       'status': 'unknown-needs-reconciliation'}})
        self.assertEqual(claimed_post_count('2026-10-10', reacted), 0)

    def test_naive_clock_is_rejected(self):
        with self.assertRaises(ValueError):
            due_slots(at(9).replace(tzinfo=None), POLICY, state())

    def test_invalid_policy_fails_closed(self):
        for override in ({'posting_hours': [9, 9]}, {'posting_hours': [True]},
                         {'posting_days': [7]}, {'posting_max_delay_minutes': 121},
                         {'max_posts_per_day': -1}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                due_slots(at(9), {**POLICY, **override}, state())

    def test_invalid_counter_fails_closed(self):
        with self.assertRaises(ValueError):
            due_slots(at(9), POLICY, state(count='0'))


if __name__ == '__main__':
    unittest.main()
