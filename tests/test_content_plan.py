import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContentPlanContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads((ROOT / 'automation/content-plan.json').read_text(encoding='utf-8'))

    def test_exact_thirty_day_campaign_has_two_unique_daily_slots(self):
        self.assertEqual(self.plan['version'], 1)
        self.assertEqual(self.plan['start_date'], '2026-10-10')
        self.assertEqual(self.plan['end_date'], '2026-11-08')
        expected = {
            ((date(2026, 10, 10) + timedelta(days=day)).isoformat(), hour)
            for day in range(30) for hour in (9, 18)
        }
        actual = [(slot['date'], slot['hour']) for slot in self.plan['slots']]
        self.assertEqual(len(actual), 60)
        self.assertEqual(len(set(actual)), 60)
        self.assertEqual(set(actual), expected)

    def test_machine_topics_match_all_sixty_documented_angles(self):
        document = (ROOT / 'docs/30-DAY-GROWTH-PLAN.md').read_text(encoding='utf-8')
        rows = [line for line in document.splitlines() if line.startswith('| ') and ' · ' in line]
        topics = []
        for day, row in enumerate(rows):
            calendar_date = date(2026, 10, 10) + timedelta(days=day)
            self.assertIn(f'{calendar_date:%a}, {calendar_date:%b} {calendar_date.day}', row)
            cells = [cell.strip() for cell in row.split('|')[1:-1]]
            for cell in cells[1:]:
                match = re.fullmatch(r'\*\*(.+?):\*\* (.+)', cell)
                self.assertIsNotNone(match)
                topics.append(match.group(2))
        self.assertEqual([slot['topic'] for slot in self.plan['slots']], topics)
        self.assertTrue(all(isinstance(slot['format'], str) and slot['format'] for slot in self.plan['slots']))

    def test_consulting_and_role_invitations_have_weekly_limits(self):
        kinds = Counter(slot['format'] for slot in self.plan['slots'])
        self.assertEqual(kinds['consulting-invitation'], 8)
        self.assertEqual(kinds['founding-designer-invitation'], 4)
        for slot in self.plan['slots']:
            weekday = date.fromisoformat(slot['date']).weekday()
            if slot['format'] == 'consulting-invitation':
                self.assertIn(weekday, (1, 3))
                self.assertEqual(slot['hour'], 18)
                self.assertTrue(slot.get('cta'))
            elif slot['format'] == 'founding-designer-invitation':
                self.assertEqual(weekday, 4)
                self.assertEqual(slot['hour'], 18)
                self.assertTrue(slot.get('cta'))
            else:
                self.assertNotIn('cta', slot)

    def test_profile_payload_uses_exact_prepared_copy_with_valid_lengths(self):
        payload = json.loads((ROOT / 'automation/profile-update.json').read_text(encoding='utf-8'))
        document = (ROOT / 'docs/PROFILE-DRAFT.md').read_text(encoding='utf-8')
        headline = document.split('## Headline\n\n', 1)[1].split('\n\n', 1)[0]
        summary = document.split('## About\n\n', 1)[1].split('\n\n## Banner', 1)[0]
        self.assertEqual(payload, {'headline': headline, 'summary': summary})
        self.assertLessEqual(len(headline), 220)
        self.assertGreaterEqual(len(summary.split()), 200)
        self.assertLessEqual(len(summary.split()), 300)


if __name__ == '__main__':
    unittest.main()
