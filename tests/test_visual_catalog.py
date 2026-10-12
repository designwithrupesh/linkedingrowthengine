"""Checks for the reviewed campaign's public copy and attachment references."""
import json
from pathlib import Path
import re
import unittest

from lib.voice import violations


ROOT = Path(__file__).resolve().parents[1]


class VisualCatalogContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / 'automation/visual-content.json').read_text(encoding="utf-8"))
        cls.plan = json.loads((ROOT / 'automation/content-plan.json').read_text(encoding="utf-8"))

    def test_future_media_slots_resolve_to_unique_reviewed_assets(self):
        assets = self.catalog['assets']
        by_id = {asset['id']: asset for asset in assets}
        self.assertEqual(len(by_id), len(assets))
        used = []
        for slot in self.plan['slots']:
            if 'media_ref' not in slot:
                continue
            self.assertGreaterEqual((slot['date'], slot['hour']), ('2026-10-12', 18))
            self.assertNotIn((slot['date'], slot['hour']),
                             {('2026-10-13', 9), ('2026-10-14', 9)})
            self.assertIn(slot['media_ref'], by_id)
            self.assertEqual(slot['format'], by_id[slot['media_ref']]['kind'])
            used.append(slot['media_ref'])
        self.assertEqual(len(used), len(set(used)), 'A starter asset must not repeat in the campaign')
        self.assertEqual(set(used), set(by_id), 'Every reviewed starter asset has a planned slot')
        self.assertEqual({by_id[key]['kind'] for key in used}, {'image', 'carousel', 'freebie'})

    def test_public_copy_passes_owner_voice_and_has_readable_card_lengths(self):
        for asset in self.catalog['assets']:
            with self.subTest(asset=asset['id']):
                self.assertRegex(asset['id'], r'^[a-z][a-z0-9-]{2,70}$')
                self.assertIn(asset['kind'], {'image', 'carousel', 'freebie'})
                self.assertGreaterEqual(len(asset['caption']), 400)
                self.assertLessEqual(len(asset['caption']), 1000)
                copy = [asset['title'], asset.get('subtitle', ''), asset['caption']]
                for slide in asset['slides']:
                    self.assertLessEqual(len(slide['title']), 70)
                    self.assertGreaterEqual(len(slide['body']), 1)
                    self.assertLessEqual(len(slide['body']), 4)
                    self.assertTrue(all(len(line) <= 95 for line in slide['body']))
                    copy.extend([slide['title'], slide.get('example', ''), *slide['body']])
                for text in copy:
                    if text:
                        self.assertFalse(violations(text), (asset['id'], violations(text)))

    def test_carousels_have_a_sequence_and_freebies_have_working_fields(self):
        for asset in self.catalog['assets']:
            with self.subTest(asset=asset['id']):
                if asset['kind'] == 'image':
                    self.assertEqual(len(asset['slides']), 1)
                else:
                    self.assertGreaterEqual(len(asset['slides']), 4)
                    self.assertLessEqual(len(asset['slides']), 7)
                if asset['kind'] == 'freebie':
                    self.assertEqual(asset['download_slug'], asset['id'])
                    fields = [line for slide in asset['slides'] for line in slide['body']]
                    self.assertGreaterEqual(len(fields), 15)
                    # The worksheet layout supplies ruled blank answer spaces.
                    # Public copy contains prompts, rather than fake examples.
                    self.assertTrue(all(slide.get('layout') == 'worksheet'
                                        and len(slide['body']) <= 3
                                        for slide in asset['slides']))
                    self.assertTrue(all('[' not in line for line in fields))

    def test_format_change_keeps_the_original_editorial_intent_and_cta(self):
        future = [slot for slot in self.plan['slots']
                  if (slot['date'], slot['hour']) >= ('2026-10-12', 18)
                  and (slot['date'], slot['hour']) not in
                  {('2026-10-13', 9), ('2026-10-14', 9)}]
        self.assertTrue(all(slot.get('editorial_format') for slot in future))
        consulting = next(slot for slot in future
                          if slot['date'] == '2026-10-13' and slot['hour'] == 18)
        self.assertEqual(consulting['format'], 'carousel')
        self.assertEqual(consulting['editorial_format'], 'consulting-invitation')
        self.assertIn(consulting['cta'],
                      next(item for item in self.catalog['assets']
                           if item['id'] == consulting['media_ref'])['caption'])


if __name__ == '__main__':
    unittest.main()
