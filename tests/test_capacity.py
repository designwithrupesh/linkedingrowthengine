from datetime import datetime, timezone
import os
import unittest
from unittest.mock import Mock, patch

import requests

from automation.capacity import ACCOUNT_CONTEXT_URL, UNVERIFIED, check_post_capacity


PLATFORM = 'linkedin-testConnection'
SCHEDULE = datetime(2026, 10, 9, 3, 30, tzinfo=timezone.utc)


def snapshot():
    return {'success': True, 'context': {
        'serverTime': '2026-10-08T19:16:01.505Z',
        'features': {'publishing': True, 'apiAccess': True},
        'allowedPlatforms': ['linkedin'],
        'quotas': {
            'monthlyPosts': {'scope': 'account', 'limit': 15, 'used': 3, 'remaining': 12},
            'scheduledPosts': {'scope': 'account', 'limit': 3, 'used': 2, 'remaining': 1},
            'scheduleHorizon': {'limit': 7, 'maxScheduledDate': '2026-10-15T23:59:59.999Z'},
        },
    }}


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'PUBLORA_API_KEY': 'test-credential'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.request = patch('automation.capacity.requests.get').start()
        self.addCleanup(patch.stopall)
        self.request.return_value = Mock(status_code=200)

    def check(self, data=None, scheduled=SCHEDULE):
        self.request.return_value.json.return_value = data if data is not None else snapshot()
        return check_post_capacity(PLATFORM, scheduled)

    def test_available_starter_capacity_uses_verified_read_only_request(self):
        self.assertTrue(self.check()[0])
        args, kw = self.request.call_args
        self.assertEqual(args, (ACCOUNT_CONTEXT_URL,))
        self.assertFalse(kw['allow_redirects'])
        self.assertEqual(kw['params'], {'scheduledTime': SCHEDULE.isoformat()})
        self.assertEqual(kw['timeout'], 20)

    def test_real_starter_full_queue_defers_without_consuming_capacity(self):
        data = snapshot()
        data['context']['quotas']['scheduledPosts'].update(used=3, remaining=0)
        allowed, reason = self.check(data)
        self.assertFalse(allowed)
        self.assertIn('queue is full', reason)

    def test_monthly_quota_reset_is_required_when_full(self):
        data = snapshot()
        data['context']['quotas']['monthlyPosts'].update(used=15, remaining=0)
        self.assertIn('monthly publishing quota is full', self.check(data)[1])

    def test_free_plan_horizon_blocks_later_schedule(self):
        self.assertFalse(self.check(scheduled=datetime(2026, 10, 16, tzinfo=timezone.utc))[0])

    def test_horizon_boundary_is_allowed(self):
        self.assertTrue(self.check(scheduled='2026-10-15T23:59:59.999Z')[0])

    def test_past_schedule_is_blocked_using_service_clock(self):
        self.assertFalse(self.check(scheduled='2026-10-07T03:30:00Z')[0])

    def test_paid_unlimited_plan_limits_remain_unlimited(self):
        data = snapshot()
        data['context']['allowedPlatforms'] = None
        for name in ('monthlyPosts', 'scheduledPosts'):
            data['context']['quotas'][name].update(limit=None, remaining=None)
        data['context']['quotas']['scheduleHorizon'].update(limit=None, maxScheduledDate=None)
        self.assertTrue(self.check(data, scheduled='2027-10-09T03:30:00Z')[0])

    def test_connection_monthly_quota_uses_specific_account(self):
        data = snapshot()
        data['context']['quotas']['monthlyPosts'] = {
            'scope': 'connection', 'limit': 10, 'remaining': None,
            'connections': [{'platformSelection': PLATFORM, 'limit': 10, 'used': 10, 'remaining': 0},
                            {'platformSelection': 'linkedin-other', 'limit': 10, 'used': 0, 'remaining': 10}]}
        self.assertFalse(self.check(data)[0])
        data['context']['quotas']['monthlyPosts']['connections'][0].update(used=9, remaining=1)
        self.assertTrue(self.check(data)[0])

    def test_missing_connection_quota_fails_closed(self):
        data = snapshot()
        data['context']['quotas']['monthlyPosts'].update(scope='connection', connections=[])
        self.assertEqual(self.check(data), (False, UNVERIFIED))

    def test_unknown_malformed_or_inconsistent_service_quota_fails_closed(self):
        for change in ('missing-context', 'missing-monthly', 'boolean-remaining', 'inconsistent', 'unknown-scope'):
            data = snapshot()
            if change == 'missing-context':
                del data['context']
            elif change == 'missing-monthly':
                del data['context']['quotas']['monthlyPosts']
            else:
                row = data['context']['quotas']['monthlyPosts']
                row.update({'boolean-remaining': {'remaining': True},
                            'inconsistent': {'used': 15, 'remaining': 1},
                            'unknown-scope': {'scope': 'unknown'}}[change])
            with self.subTest(change=change):
                self.assertEqual(self.check(data), (False, UNVERIFIED))

    def test_denied_features_and_platform_fail_closed(self):
        for change in ('publishing', 'apiAccess', 'allowedPlatforms'):
            data = snapshot()
            if change == 'allowedPlatforms':
                data['context'][change] = ['instagram']
            else:
                data['context']['features'][change] = False
            with self.subTest(change=change):
                self.assertFalse(self.check(data)[0])

    def test_redirects_errors_and_network_failures_do_not_expose_credentials(self):
        for status in (301, 403, 429, 500):
            self.request.return_value.status_code = status
            self.assertEqual(self.check(), (False, UNVERIFIED))
        self.request.side_effect = requests.Timeout('secret test-credential response body')
        self.assertEqual(self.check(), (False, UNVERIFIED))

    def test_naive_schedule_and_missing_credentials_avoid_request(self):
        self.assertFalse(self.check(scheduled=datetime(2026, 10, 9))[0])
        self.request.assert_not_called()
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self.check()[0])
        self.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
