from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from automation.apify_ai import ApifyBudgetError
from automation import visual_planning as planning
from automation.visual_assets import VisualAssetError
from lib.voice import VoiceError, prompt_rules


SPEC = {
    "id": "design-review-card", "kind": "image",
    "title": "Ask what the screen helps someone finish",
    "subtitle": "A practical design review card",
    "caption": "I'd start a design review with one customer task. Find the next action on the screen, then check whether the surrounding information helps someone choose it.",
    "slides": [{"title": "Keep the task visible", "body": [
        "Name what the person is trying to finish.",
        "Show the next action clearly.",
        "Check what information helps that choice."
    ]}],
}
METADATA = {"path": "/tmp/card.png", "preview_path": "/tmp/card.png",
            "mime_type": "image/png", "title": SPEC["title"],
            "sha256": "a" * 64, "page_count": 1}


class VisualFormatTests(unittest.TestCase):
    def test_default_weekly_evening_rotation_and_text_mornings(self):
        monday = date(2026, 10, 12)
        for offset, kind in enumerate(planning.DEFAULT_WEEKDAY_FORMATS):
            day = date.fromordinal(monday.toordinal() + offset)
            self.assertEqual(planning.select_brief_format({}, day, 18), kind)
            self.assertEqual(planning.select_brief_format({}, day, 9), "text")

    def test_explicit_format_overrides_time_rotation(self):
        for kind in planning.FORMATS:
            self.assertEqual(planning.select_brief_format({}, "2026-10-12", 9,
                                                         {"format": kind}), kind)
        self.assertEqual(planning.select_brief_format({}, "2026-10-12", 18,
                                                     {"format": "text"}), "text")

    def test_legacy_prose_labels_are_not_reinterpreted_as_media(self):
        for kind in ("checklist", "walkthrough", "framework", "rubric", "position", None, []):
            self.assertEqual(planning.select_brief_format({}, "2026-10-12", 18,
                                                         {"format": kind}), "text")

    def test_policy_rotation_is_supported_and_validated(self):
        policy = {"visual_formats_by_weekday": ["freebie"] * 7}
        self.assertEqual(planning.select_brief_format(policy, "2026-10-12", 18), "freebie")
        for rotation in (["text"] * 6, ["photo"] * 7, {"0": "image"}, [None] * 7):
            with self.subTest(rotation=rotation), self.assertRaises(VisualAssetError):
                planning.select_brief_format({"visual_formats_by_weekday": rotation},
                                             "2026-10-12", 18)

    def test_enforced_rotation_cannot_be_erased_by_legacy_text_briefs(self):
        policy = {'enforce_visual_rotation': True}
        monday = date(2026, 10, 12)
        formats = []
        for offset, kind in enumerate(planning.DEFAULT_WEEKDAY_FORMATS):
            day = date.fromordinal(monday.toordinal() + offset)
            formats.append(planning.select_brief_format(policy, day, 18, {'format': 'text'}))
            self.assertEqual(formats[-1], kind)
            self.assertEqual(planning.select_brief_format(policy, day, 9, {'format': 'text'}), 'text')
        self.assertEqual(formats.count('carousel'), 3)
        self.assertEqual(formats.count('freebie'), 2)
        self.assertEqual(formats.count('image'), 2)
        self.assertNotIn('text', formats)

    def test_enforced_rotation_keeps_explicit_reviewed_formats_and_text_exceptions(self):
        policy = {'enforce_visual_rotation': True}
        self.assertEqual(planning.select_brief_format(policy, '2026-10-12', 18,
                                                     {'format': 'freebie', 'art_ref': 'reviewed'}), 'freebie')
        self.assertEqual(planning.select_brief_format(policy, '2026-10-12', 18,
                                                     {'format': 'text', 'allow_text_exception': True}), 'text')
        for override in ({'enforce_visual_rotation': 'true'},
                         {'enforce_visual_rotation': True, 'visual_formats_by_weekday': ['text'] * 7}):
            with self.subTest(override=override), self.assertRaises(VisualAssetError):
                planning.select_brief_format(override, '2026-10-12', 18, {'format': 'text'})

    def test_enforcement_activation_keeps_historical_text_and_applies_on_start_date(self):
        policy = {'enforce_visual_rotation': True, 'visual_rotation_start_date': '2026-10-26'}
        self.assertEqual(planning.select_brief_format(policy, '2026-10-25', 18, {'format': 'text'}), 'text')
        self.assertEqual(planning.select_brief_format(policy, '2026-10-26', 18, {'format': 'text'}), 'image')
        self.assertEqual(planning.select_brief_format(policy, '2026-10-27', 18, {'format': 'text'}), 'carousel')
        self.assertEqual(planning.select_brief_format(policy, '2026-10-25', 18, {'format': 'freebie'}), 'freebie')
        for invalid in ('2026-10-32', '20261026', None, 26):
            with self.subTest(activation=invalid), self.assertRaises(VisualAssetError):
                planning.select_brief_format({**policy, 'visual_rotation_start_date': invalid},
                                             '2026-10-26', 18, {'format': 'text'})

    def test_invalid_dates_hours_and_briefs_fail_before_selection(self):
        for day in ("2026-10-32", "20261012", 12):
            with self.subTest(day=day), self.assertRaises(VisualAssetError):
                planning.select_brief_format({}, day, 18)
        for hour in (True, -1, 24, "18"):
            with self.subTest(hour=hour), self.assertRaises(VisualAssetError):
                planning.select_brief_format({}, "2026-10-12", hour)
        with self.assertRaises(VisualAssetError):
            planning.select_brief_format({}, "2026-10-12", 18, [])


class VisualPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = Path(self.temp.name) / "catalog.json"
        self.spec = deepcopy(SPEC)
        self.write_catalog(self.spec)
        self.runner = SimpleNamespace(policy={"background": ["product design"], "source_notes": []},
                                      state={"actions": {}}, model_paused=False)
        self.render = Mock(return_value=deepcopy(METADATA))
        self.catalog_patch = patch.object(planning, "CATALOG_PATH", self.catalog)
        self.render_patch = patch.object(planning, "render_asset", self.render)
        self.catalog_patch.start()
        self.render_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.addCleanup(self.render_patch.stop)

    def write_catalog(self, *specs):
        self.catalog.write_text(json.dumps({"version": 1, "assets": list(specs)}), encoding="utf-8")

    def prepare(self, kind="image", **brief):
        return planning.prepare_visual(self.runner,
                                       {"format": kind, "media_ref": self.spec["id"], **brief},
                                       "2026-10-12", 18, self.temp.name)

    def generated(self, kind="image"):
        spec = deepcopy(self.spec)
        spec.update(id=f"visual-2026-10-12-1800-{kind}", kind=kind,
                    subtitle="Hypothetical example: choosing an appointment")
        layouts = (["annotated-ui"] if kind == "image"
                   else ["worksheet"] * 4 if kind == "freebie"
                   else ["comparison", "diagram", "matrix", "worksheet"])
        visuals = {
            "annotated-ui": {
                "screens": [
                    {"label": "Before", "title": "Schedule an appointment", "rows": ["Weekly calendar"], "action": "View calendar"},
                    {"label": "After", "title": "Choose an appointment", "rows": ["Next available: Tuesday"], "action": "Choose this time"},
                ],
                "annotations": [{"label": "Priority", "detail": "The booking decision is visible first."}],
            },
            "comparison": {"panels": [
                {"label": "Before", "title": "Weekly calendar", "body": "Find a day before seeing an appointment."},
                {"label": "After", "title": "Next available time", "body": "Choose a time before opening the full calendar."},
            ]},
            "diagram": {"nodes": [
                {"label": "First", "title": "Choose a time", "detail": "Show the next available appointment."},
                {"label": "Next", "title": "Confirm the booking", "detail": "Explain the duration before committing."},
            ]},
            "matrix": {"matrix": {"columns": ["Moment", "Decision"], "rows": [
                ["Choose time", "Show next available"], ["Compare days", "Open full calendar"]]}},
            "worksheet": {"fields": [
                {"label": "Customer task", "hint": "Example: book an appointment."},
                {"label": "First decision", "hint": "Example: choose the next available time."},
            ]},
        }
        spec["slides"] = [{"title": "Keep the task visible", "body": ["Name the decision this screen should make easier."],
                           "layout": layout, "visual": deepcopy(visuals[layout])} for layout in layouts]
        return spec

    def generation(self, spec=None, **outer):
        payload = {"text": json.dumps(spec if spec is not None else self.generated()),
                   "skip": False, "reason": "", **outer}
        return patch("automation.twin.complete", return_value=payload)

    def runtime_prepare(self, kind="image"):
        return planning.prepare_visual(self.runner, {"format": kind, "topic": "Review the customer task"},
                                       "2026-10-12", 18, self.temp.name)

    def test_catalog_renders_without_model_or_state_mutation(self):
        state = deepcopy(self.runner.state)
        source = self.catalog.read_bytes()
        with patch("automation.twin.complete") as complete:
            asset = self.prepare()
        complete.assert_not_called()
        self.assertEqual(asset["asset_id"], self.spec["id"])
        self.assertEqual(asset["caption"], self.spec["caption"])
        self.assertEqual(asset["mime_type"], "image/png")
        self.assertEqual(asset["spec"]["caption"], self.spec["caption"])
        self.assertEqual(asset["spec"]["slides"][0]["layout"], "checklist")
        self.assertIsNone(asset["download_url"])
        self.assertEqual(self.runner.state, state)
        self.assertEqual(self.catalog.read_bytes(), source)
        self.render.assert_called_once()

    def test_catalog_available_when_model_budget_is_paused(self):
        self.runner.model_paused = True
        with patch("automation.twin.complete") as complete:
            self.assertIsNotNone(self.prepare())
            self.assertIsNone(self.runtime_prepare())
        complete.assert_not_called()

    def test_catalog_freebie_uses_real_public_resource_link(self):
        self.spec["kind"] = "freebie"
        self.write_catalog(self.spec)
        asset = self.prepare("freebie")
        self.assertEqual(asset["download_url"], planning.PUBLIC_DOWNLOAD_BASE + self.spec["id"] + ".pdf")
        self.assertTrue(asset["caption"].endswith(asset["download_url"]))
        self.assertIn("Download the worksheet:", asset["caption"])

    def test_catalog_reference_errors_never_fall_back_to_generation(self):
        for reference in ("missing", None, ""):
            with self.subTest(reference=reference), patch("automation.twin.complete") as complete:
                with self.assertRaises(VisualAssetError):
                    self.prepare(media_ref=reference)
                complete.assert_not_called()
        self.write_catalog(self.spec, self.spec)
        with self.assertRaises(VisualAssetError):
            self.prepare()
        self.write_catalog(self.spec)
        with self.assertRaises(VisualAssetError):
            self.prepare("freebie")
        self.render.assert_not_called()

    def test_all_post_action_statuses_reserve_asset_on_other_dates(self):
        for status in ("scheduled", "published", "rejected", "inflight", "unknown-needs-reconciliation"):
            self.runner.state["actions"] = {"post:2026-10-11:1800": {
                "kind": "post", "status": status, "asset_id": self.spec["id"],
                "allocation_day": "2026-10-11"}}
            with self.subTest(status=status), self.assertRaises(VisualAssetError):
                self.prepare()
        self.render.assert_not_called()

    def test_visible_copy_on_every_slide_uses_voice_and_grounding_checks(self):
        for change, error in (
            (lambda s: s["slides"][0]["body"].__setitem__(1, "Keep the next action clear\u2014then test it."), VoiceError),
            (lambda s: s["slides"][0].__setitem__("title", "I helped my clients increase conversions."), VisualAssetError),
            (lambda s: s.__setitem__("caption", "I've redesigned this for our clients."), VisualAssetError),
            (lambda s: s["slides"][0].__setitem__("example", "We tested this with our customers."), VisualAssetError),
        ):
            spec = deepcopy(SPEC)
            change(spec)
            self.write_catalog(spec)
            with self.subTest(error=error), self.assertRaises(error):
                self.prepare()
        self.render.assert_not_called()

    def test_confirmed_source_note_can_support_a_visible_owner_claim(self):
        self.spec["caption"] = "I've worked in product design."
        self.runner.policy["source_notes"] = [self.spec["caption"]]
        self.write_catalog(self.spec)
        self.assertEqual(self.prepare()["caption"], self.spec["caption"])

    def test_text_selection_does_not_render_or_generate(self):
        with patch("automation.twin.complete") as complete:
            self.assertIsNone(self.prepare("text"))
        complete.assert_not_called()
        self.render.assert_not_called()

    def test_runtime_generates_once_with_full_voice_only_once_and_no_old_posts(self):
        self.runner.state["actions"]["old-post"] = {"kind": "post", "text": "BAD_OLD_COPY"}
        with patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                self.generation() as complete:
            asset = self.runtime_prepare()
        self.assertEqual(asset["id"], "visual-2026-10-12-1800-image")
        complete.assert_called_once()
        messages = complete.call_args.args[4]
        self.assertEqual(messages[0]["content"].count(prompt_rules()), 1)
        self.assertIn('VISUAL CAPTION', messages[0]['content'])
        self.assertIn('Caption the design you are specifying', messages[0]['content'])
        self.assertNotIn("BAD_OLD_COPY", json.dumps(messages))
        self.assertEqual(complete.call_args.kwargs["kind"], "generic")

    def source(self):
        return {'publisher': 'Linear', 'source_kind': 'product_update',
                'title': 'Stacked pull requests',
                'excerpt': 'Related changes stay connected during review through explicit dependencies.',
                'url': 'https://linear.app/changelog/2026-10-08-stacked-pull-requests',
                'published_at': '2026-10-08T15:44:14+00:00'}

    def source_spec(self):
        spec = self.generated()
        spec.update(title='Review related changes',
                    subtitle='Conceptual illustration: stacked pull requests',
                    caption="I'd keep the dependency visible beside the change being reviewed.")
        spec['slides'] = [{'title': 'Stacked pull requests',
                           'body': ['Related changes stay connected during review.'],
                           'layout': 'diagram', 'visual': {'nodes': [
                               {'label': 'Change', 'title': 'Related work', 'detail': 'Keep the dependency explicit.'},
                               {'label': 'Review', 'title': 'Connected changes', 'detail': 'Show what the review depends on.'},
                           ]}}]
        return spec

    def test_current_source_visual_is_a_labeled_specific_diagram_with_exact_citation(self):
        brief = {'format': 'image', 'topic': 'Review dependencies', 'current_design_source': self.source()}
        original = deepcopy(brief)
        with patch('automation.twin.model_connection', return_value=('endpoint', 'token', 'model', False)), \
             self.generation(self.source_spec()) as complete:
            asset = planning.prepare_visual(self.runner, brief, '2026-10-12', 18, self.temp.name)
        self.assertEqual(asset['kind'], 'image')
        self.assertIn(self.source()['url'], asset['caption'])
        self.assertEqual(asset['spec']['slides'][0]['layout'], 'diagram')
        self.assertIn('Conceptual illustration', asset['spec']['subtitle'])
        context = json.loads(complete.call_args.args[4][1]['content'].split('\n', 1)[1])
        self.assertEqual(context['current_design_source']['published_at'], self.source()['published_at'])
        self.assertTrue(context['current_design_source']['data_only'])
        self.assertEqual(context['attachment']['kind'], 'image')
        self.assertIn('not a screenshot', complete.call_args.args[4][0]['content'])
        self.assertIn('VISUAL CAPTION', complete.call_args.args[4][0]['content'])
        self.assertNotIn('Choose a relevant appointment', complete.call_args.args[4][0]['content'])
        system = complete.call_args.args[4][0]['content']
        for seed in ('calendar', 'appointment', 'booking', 'Book this time', 'View calendar'):
            self.assertNotIn(seed.casefold(), system.casefold())
        encoded = system.split('Encoding skeleton only, not copy to publish: ', 1)[1]
        skeleton, _ = json.JSONDecoder().raw_decode(encoded)
        schematic = json.loads(skeleton['text'])
        self.assertEqual(schematic['title'], 'Source-grounded title')
        self.assertEqual(schematic['caption'], 'Source-grounded caption')
        self.assertEqual(schematic['subtitle'], 'Conceptual illustration')
        self.assertEqual(schematic['slides'], [])
        self.assertEqual(brief, original)

    def test_source_visual_holds_unrelated_content_or_unsupported_numbers_without_text_fallback(self):
        unrelated = self.source_spec()
        unrelated.update(title='Choose an appointment', subtitle='Conceptual illustration: booking')
        unrelated['slides'] = [{**slide, 'title': 'Choose an appointment',
                                'body': ['The calendar opens first.'], 'visual': {'nodes': [
                                    {'label': 'Time', 'title': 'Choose a time', 'detail': 'Select Tuesday.'},
                                    {'label': 'Book', 'title': 'Confirm the booking', 'detail': 'Read the calendar.'},
                                ]}} for slide in unrelated['slides']]
        invented = self.source_spec()
        invented['slides'][0]['body'] = ['Related changes make review 37% faster.']
        for spec in (unrelated, invented):
            with self.subTest(title=spec['title']), \
                 patch('automation.twin.model_connection', return_value=('endpoint', 'token', 'model', False)), \
                 self.generation(spec), self.assertRaises(VisualAssetError):
                planning.prepare_visual(self.runner, {'format': 'image', 'current_design_source': self.source()},
                                        '2026-10-12', 18, self.temp.name)
        self.render.assert_not_called()

    def test_source_carousel_keeps_pdf_format_and_source_specific_content_on_each_page(self):
        spec = self.generated('carousel')
        spec.update(title='Review related changes',
                    subtitle='Conceptual illustration: stacked pull requests',
                    caption='The dependency belongs beside the change being reviewed.')
        spec['slides'] = [
            {'title': 'Two ways to describe related work', 'body': ['Keep connected changes visible.'],
             'layout': 'comparison', 'visual': {'panels': [
                 {'label': 'Separate', 'title': 'Changes alone', 'body': 'The review does not show a dependency.'},
                 {'label': 'Connected', 'title': 'Related changes', 'body': 'The dependency is explicit.'}]}},
            self.source_spec()['slides'][0],
            {'title': 'What connects the review', 'body': ['Name the explicit dependency.'],
             'layout': 'matrix', 'visual': {'matrix': {'columns': ['Work', 'Review'],
                                                      'rows': [['Related changes', 'Explicit dependencies']]}}},
            {'title': 'A question for the review', 'body': ['Keep related work in view.'],
             'layout': 'worksheet', 'visual': {'fields': [{'label': 'Dependency',
                                                          'hint': 'Which related change affects this review?'}]}},
        ]
        with patch('automation.twin.model_connection', return_value=('endpoint', 'token', 'model', False)), \
             self.generation(spec):
            asset = planning.prepare_visual(self.runner, {'format': 'carousel', 'current_design_source': self.source()},
                                            '2026-10-12', 18, self.temp.name)
        self.assertEqual(asset['kind'], 'carousel')
        self.assertEqual(len(asset['spec']['slides']), 4)
        self.assertIn(self.source()['url'], asset['caption'])
        unrelated_page = deepcopy(spec)
        unrelated_page['slides'][3].update(body=['Choose an appointment.'], visual={'fields': [
            {'label': 'Time', 'hint': 'Choose Tuesday.'}]})
        with patch('automation.twin.model_connection', return_value=('endpoint', 'token', 'model', False)), \
             self.generation(unrelated_page), self.assertRaisesRegex(VisualAssetError, 'unrelated content page'):
            planning.prepare_visual(self.runner, {'format': 'carousel', 'current_design_source': self.source()},
                                    '2026-10-12', 18, self.temp.name)

    def test_source_cannot_be_attached_to_an_unrelated_reviewed_catalog_asset(self):
        with patch('automation.twin.complete') as complete, self.assertRaises(VisualAssetError):
            self.prepare(current_design_source=self.source())
        complete.assert_not_called()
        self.render.assert_not_called()

    def test_generation_prompt_has_a_valid_encoded_json_skeleton(self):
        with patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                self.generation() as complete:
            self.runtime_prepare()
        system = complete.call_args.args[4][0]["content"]
        sample_source = system.split("Encoding skeleton only, not copy to publish: ", 1)[1]
        outer, consumed = json.JSONDecoder().raw_decode(sample_source)
        inner = json.loads(outer["text"])
        self.assertEqual(set(outer), {"text", "skip", "reason"})
        self.assertEqual(inner["id"], "visual-2026-10-12-1800-image")
        self.assertEqual(inner["kind"], "image")
        self.assertEqual(inner["slides"], [])
        self.assertIn("Replace its empty slides", sample_source[consumed:])
        self.assertIn("write examples as ordinary unquoted words", system)

    def test_unescaped_quotes_in_nested_json_are_held_without_repair_or_retry(self):
        # This was observed in a provider response. The outer text envelope
        # was valid, but a quoted example broke the inner content JSON.
        spec = self.generated("carousel")
        source = json.dumps(spec).replace("Example: book an appointment.",
                                          'Example: "book an appointment".')
        self.assertNotEqual(source, json.dumps(spec))
        with patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                self.generation(spec, text=source) as complete, \
                self.assertRaisesRegex(VisualAssetError, "JSON specification"):
            self.runtime_prepare("carousel")
        complete.assert_called_once()
        self.render.assert_not_called()

    def test_runtime_freebie_attaches_pdf_without_inventing_download_link(self):
        with patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                self.generation(self.generated("freebie")):
            asset = self.runtime_prepare("freebie")
        self.assertIsNone(asset["download_url"])
        self.assertEqual(asset["caption"], self.spec["caption"])
        self.assertEqual(len(self.render.call_args.args[0]["slides"]), 4)

    def test_runtime_skip_does_not_render(self):
        with patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                self.generation(text="", skip=True, reason="No useful factual topic"):
            self.assertIsNone(self.runtime_prepare())
        self.render.assert_not_called()

    def test_runtime_budget_error_propagates_without_retry_or_render(self):
        with patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                patch("automation.twin.complete", side_effect=ApifyBudgetError("Credit paused")) as complete:
            with self.assertRaises(ApifyBudgetError):
                self.runtime_prepare()
        complete.assert_called_once()
        self.render.assert_not_called()

    def test_runtime_rejects_fenced_json_and_forged_identity(self):
        for spec, outer in (
            (self.generated(), {"text": "```json\n{}\n```"}),
            ({**self.generated(), "id": "another-owner-id"}, {}),
            ({**self.generated(), "kind": "freebie"}, {}),
            ({**self.generated(), "extra": "ignored instruction"}, {}),
            ({**self.generated(), "slides": []}, {}),
        ):
            with self.subTest(spec=spec, outer=outer), \
                    patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                    self.generation(spec, **outer), self.assertRaises(VisualAssetError):
                self.runtime_prepare()
        self.render.assert_not_called()

    def test_runtime_rejects_invalid_model_envelope_before_rendering(self):
        for payload in ({"text": "{}", "skip": "false", "reason": ""},
                        {"text": "{}", "skip": False},
                        {"text": "{}", "skip": False, "reason": "", "extra": True},
                        {"text": "{}", "skip": False, "reason": "Uncertain"}, []):
            with self.subTest(payload=payload), \
                    patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                    patch("automation.twin.complete", return_value=payload), self.assertRaises(VisualAssetError):
                self.runtime_prepare()
        self.render.assert_not_called()

    def test_runtime_compact_four_page_fixture_parses_and_passes_grounding(self):
        # Exercise the nested JSON envelope using practical copy near each
        # requested limit. The character bound is a serialization estimate;
        # it is not a claim that characters precisely predict model tokens.
        fill = lambda text, limit: (text + " " + text)[:limit].rsplit(" ", 1)[0]
        spec = self.generated("carousel")
        spec["title"] = fill("Check the product promise against one complete customer task", 80)
        spec["subtitle"] = fill("Hypothetical example: compare the promise and the first product task", 100)
        spec["caption"] = fill(
            "I'd compare one customer task with the promise that brought someone to the product. "
            "Walk through the first screen and check what information they need to choose the next action. "
            "When the explanation changes, write down the exact moment and which team can fix it. "
            "The next step can be clearer wording, a changed screen or a better handoff. "
            "Choose the change after naming the gap, then check the original task again.", 550)
        for slide in spec["slides"]:
            slide["title"] = fill("Keep the customer task and the promise together", 55)
            slide["body"] = [
                fill("Record what the person expects before opening the product and choosing the next step.", 85),
            ]
        envelope = json.dumps({"text": json.dumps(spec), "skip": False, "reason": ""})
        self.assertLessEqual(len(envelope), 3500)
        self.assertEqual(json.loads(json.loads(envelope)["text"]), spec)
        with patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                self.generation(spec):
            asset = self.runtime_prepare("carousel")
        self.assertEqual(len(asset["spec"]["slides"]), 4)
        self.assertTrue(all(len(slide["body"]) == 1 for slide in asset["spec"]["slides"]))
        self.assertEqual([slide["layout"] for slide in asset["spec"]["slides"]],
                         ["comparison", "diagram", "matrix", "worksheet"])
        self.assertEqual(asset["spec"]["caption"], spec["caption"])

    def test_runtime_compact_limits_do_not_change_reviewed_catalog_specs(self):
        self.spec["slides"][0]["body"].append("Keep the team responsible for the next check visible.")
        self.write_catalog(self.spec)
        self.assertEqual(len(self.prepare()["spec"]["slides"][0]["body"]), 4)
        for change in (
            lambda spec: spec.__setitem__("caption", "a" * 551),
            lambda spec: spec["slides"][0].__setitem__("title", "a" * 86),
            lambda spec: spec["slides"][0]["body"].__setitem__(0, "a" * 161),
            lambda spec: spec["slides"][0]["body"].extend(["A second line.", "An excessive third line."]),
        ):
            spec = self.generated()
            change(spec)
            with self.subTest(spec=spec), \
                    patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                    self.generation(spec), self.assertRaises(VisualAssetError):
                self.runtime_prepare()

    def test_runtime_requires_meaningful_visual_and_hypothetical_example_label(self):
        for change in (
            lambda spec: spec.__setitem__("subtitle", "An appointment screen"),
            lambda spec: spec["slides"][0].pop("visual"),
            lambda spec: spec["slides"][0].__setitem__("visual", {}),
            lambda spec: spec["slides"][0]["visual"].__setitem__("instructions", "Do something else"),
            lambda spec: spec["slides"][0]["visual"]["screens"][0].__setitem__("action", "a" * 36),
            lambda spec: spec["slides"][0]["visual"]["screens"][0].__setitem__("rows", []),
            lambda spec: spec["slides"][0]["visual"]["annotations"].extend([
                {"label": "More", "detail": "Extra text."} for _ in range(3)]),
            lambda spec: spec["slides"][0].__setitem__("layout", "checklist"),
        ):
            spec = self.generated()
            change(spec)
            with self.subTest(spec=spec), \
                    patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                    self.generation(spec), self.assertRaises(VisualAssetError):
                self.runtime_prepare()

    def test_completed_rich_outputs_accept_two_body_lines_four_nodes_and_two_annotations(self):
        image = self.generated()
        image["slides"][0]["body"].append("The full calendar remains one step away.")
        image["slides"][0]["visual"]["annotations"].append(
            {"label": "Context", "detail": "Show duration before asking for a commitment."})
        carousel = self.generated("carousel")
        carousel["slides"][1]["body"].append("Keep the same customer task at every step.")
        carousel["slides"][1]["visual"]["nodes"].extend([
            {"label": "Then", "title": "Review the details", "detail": "Explain cancellation terms."},
            {"label": "Finish", "title": "Book the appointment", "detail": "Confirm the selected time."},
        ])
        for kind, spec in (("image", image), ("carousel", carousel)):
            with self.subTest(kind=kind), \
                    patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                    self.generation(spec):
                asset = self.runtime_prepare(kind)
            self.assertEqual(asset["spec"]["id"], spec["id"])

    def test_screen_buttons_reject_behavior_narration_even_when_short_enough(self):
        for action in ("User scans the calendar", "Customer chooses a time", "People read the list",
                       "Choose a time then read terms and confirm"):
            spec = self.generated()
            spec["slides"][0]["visual"]["screens"][0]["action"] = action
            with self.subTest(action=action), \
                    patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                    self.generation(spec), self.assertRaises(VisualAssetError):
                self.runtime_prepare()

    def test_every_nested_visible_field_is_checked_for_personal_grounding(self):
        # Exercise rendered prose in UI, annotations, comparisons, diagrams,
        # tables and worksheet hints. None is allowed to hide a client story.
        cases = [
            ("image", lambda s: s["slides"][0]["visual"]["screens"][0].__setitem__("action", "I helped my clients.")),
            ("image", lambda s: s["slides"][0]["visual"]["screens"][0]["rows"].__setitem__(0, "We tested this with our customers.")),
            ("image", lambda s: s["slides"][0]["visual"]["annotations"][0].__setitem__("detail", "I've redesigned this for our clients.")),
            ("carousel", lambda s: s["slides"][0]["visual"]["panels"][0].__setitem__("body", "I helped my clients.")),
            ("carousel", lambda s: s["slides"][1]["visual"]["nodes"][0].__setitem__("detail", "We tested this with our customers.")),
            ("carousel", lambda s: s["slides"][2]["visual"]["matrix"]["columns"].__setitem__(0, "Our clients")),
            ("carousel", lambda s: s["slides"][2]["visual"]["matrix"]["rows"][0].__setitem__(0, "I helped my clients.")),
            ("freebie", lambda s: s["slides"][0]["visual"]["fields"][0].__setitem__("hint", "I've redesigned this for our clients.")),
        ]
        for kind, change in cases:
            spec = self.generated(kind)
            change(spec)
            with self.subTest(kind=kind, spec=spec), \
                    patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                    self.generation(spec), self.assertRaisesRegex(VisualAssetError, "unsupported personal experience"):
                self.runtime_prepare(kind)
        self.render.assert_not_called()

    def test_nested_voice_errors_cannot_hide_in_visual_payloads(self):
        spec = self.generated()
        spec["slides"][0]["visual"]["annotations"][0]["detail"] = "Clear priority\u2014then the calendar."
        with patch("automation.twin.model_connection", return_value=("endpoint", "token", "model", False)), \
                self.generation(spec), self.assertRaises(VoiceError):
            self.runtime_prepare()
        self.render.assert_not_called()

    def test_renderer_failure_does_not_mutate_post_actions(self):
        self.render.side_effect = VisualAssetError("Page would overflow")
        state = deepcopy(self.runner.state)
        with self.assertRaises(VisualAssetError):
            self.prepare()
        self.assertEqual(self.runner.state, state)


if __name__ == "__main__":
    unittest.main()
