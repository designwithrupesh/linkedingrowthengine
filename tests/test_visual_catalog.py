"""Checks for the reviewed campaign's public copy and attachment references."""
import json
from pathlib import Path
import re
import unittest

from lib.voice import violations
from automation.visual_planning import _ground_spec


ROOT = Path(__file__).resolve().parents[1]


class VisualCatalogContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / 'automation/visual-content.json').read_text(encoding="utf-8"))
        cls.plan = json.loads((ROOT / 'automation/content-plan.json').read_text(encoding="utf-8"))
        cls.policy = json.loads((ROOT / 'automation/policy.json').read_text(encoding="utf-8"))

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
        deferred = [item['media_ref'] for item in self.plan.get('deferred_assets', [])]
        self.assertEqual(len(deferred), len(set(deferred)))
        self.assertFalse(set(used) & set(deferred), 'A deferred resource must not also publish')
        self.assertEqual(set(used) | set(deferred), set(by_id),
                         'Every reviewed starter asset is either planned or explicitly deferred')
        self.assertEqual({by_id[key]['kind'] for key in used}, {'image', 'carousel', 'freebie'})

    def test_public_copy_passes_owner_voice_and_grounding_including_diagrams(self):
        for asset in self.catalog['assets']:
            with self.subTest(asset=asset['id']):
                self.assertRegex(asset['id'], r'^[a-z][a-z0-9-]{2,70}$')
                self.assertIn(asset['kind'], {'image', 'carousel', 'freebie'})
                self.assertGreaterEqual(len(asset['caption']), 400)
                self.assertLessEqual(len(asset['caption']), 1200)
                _ground_spec(asset, self.policy)
                copy = [asset['title'], asset.get('subtitle', ''), asset['caption']]
                for slide in asset['slides']:
                    self.assertLessEqual(len(slide['title']), 85)
                    self.assertGreaterEqual(len(slide['body']), 1)
                    self.assertLessEqual(len(slide['body']), 4)
                    self.assertTrue(all(len(line) <= 230 for line in slide['body']))
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
                    self.assertLessEqual(len(asset['slides']), 8)
                if asset['kind'] == 'freebie':
                    self.assertEqual(asset['download_slug'], asset['id'])
                    fields = [field for slide in asset['slides']
                              for field in slide.get('visual', {}).get('fields', [])]
                    self.assertGreaterEqual(len(fields), 8)
                    self.assertTrue(all(field.get('label') and field.get('hint') for field in fields))
                    # A useful working document includes a filled example or
                    # decision diagram as well as spaces for the reader's work.
                    examples = [slide for slide in asset['slides']
                                if any(key in slide.get('visual', {})
                                       for key in ('panels', 'matrix', 'nodes', 'screens'))]
                    self.assertTrue(examples, 'Working fields need a concrete example or decision aid')

    def test_every_asset_has_a_concrete_visual_and_carousels_vary_the_explanation(self):
        for asset in self.catalog['assets']:
            with self.subTest(asset=asset['id']):
                self.assertTrue(all(slide.get('visual') for slide in asset['slides']))
                if asset['kind'] == 'carousel':
                    layouts = {slide.get('layout') for slide in asset['slides']}
                    self.assertGreaterEqual(len(layouts), 2)

    def test_authored_story_slots_resolve_without_republishing_the_displaced_resource(self):
        registry = json.loads((ROOT / 'automation/art-directed-content.json').read_text(encoding='utf-8'))
        by_id = {asset['id']: asset for asset in registry['assets']}
        slots = [slot for slot in self.plan['slots'] if 'art_ref' in slot]
        self.assertEqual({slot['art_ref'] for slot in slots}, set(by_id))
        self.assertEqual(len(slots), len(by_id))
        for slot in slots:
            self.assertNotIn('media_ref', slot)
            self.assertEqual(slot['format'], by_id[slot['art_ref']]['kind'])
            self.assertEqual(by_id[slot['art_ref']]['review_status'], 'checked')
        displaced = [item for item in self.plan['deferred_assets']
                     if item['media_ref'] == 'problem-definition-worksheet']
        self.assertEqual(len(displaced), 1)
        self.assertTrue(displaced[0]['reason'])

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
