import unittest
from datetime import datetime, timezone
from automation.bootstrap import slots

POLICY={'timezone':'Asia/Calcutta','posting_hour':9,'posting_days':[1,2,3]}

class BootstrapScheduleTests(unittest.TestCase):
    def test_starts_next_weekday_then_uses_standard_cadence(self):
        now=datetime(2026,10,8,18,30,tzinfo=timezone.utc)
        result=slots(now,3,POLICY)
        self.assertEqual([s.isoformat() for s in result], ['2026-10-09T03:30:00+00:00','2026-10-13T03:30:00+00:00','2026-10-14T03:30:00+00:00'])
    def test_never_backdates_a_slot(self):
        now=datetime(2026,10,9,4,0,tzinfo=timezone.utc)
        self.assertTrue(all(s>now for s in slots(now,3,POLICY)))
    def test_skips_weekend(self):
        result=slots(datetime(2026,10,10,0,0,tzinfo=timezone.utc),1,POLICY)
        self.assertEqual(result[0].date().isoformat(),'2026-10-12')
