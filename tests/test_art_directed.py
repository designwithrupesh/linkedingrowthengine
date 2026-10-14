from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from automation import art_directed as art
from automation import visual_planning as planning
from automation.post_queue import prepare_next_day_posts
from automation.twin import Runner
from automation.visual_assets import VisualAssetError
from lib.voice import VoiceError


class ArtDirectedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.resources = self.root / "resources" / "linkedin" / "stories"
        self.resources.mkdir(parents=True)
        self.registry = self.root / "automation" / "art-directed-content.json"
        self.registry.parent.mkdir()
        self.image = self.resources / "the-next-question.png"
        Image.new("RGB", (108, 135), "white").save(self.image)
        self.preview = self.resources / "the-next-question-preview.png"
        Image.new("RGB", (108, 135), "blue").save(self.preview)
        self.policy = {"background": ["product design"], "source_notes": []}
        self.entry = {
            "id": "the-next-question", "kind": "image",
            "title": "Give the next question a reason",
            "caption": "I'd explain why the delivery address matters before asking for it.",
            "file": "resources/linkedin/stories/the-next-question.png",
            "preview": "resources/linkedin/stories/the-next-question-preview.png",
            "sha256": hashlib.sha256(self.image.read_bytes()).hexdigest(),
            "page_count": 1,
            "transcript": ["Where should it arrive?", "Your address helps us show delivery options."],
            "design_brief": {
                "reader_tension": "A person hesitates to share their address.",
                "intended_feeling": "The next step feels understandable.",
                "core_decision": "Explain the question before asking it.",
                "visual_idea": "One unfinished form gives way to a clear delivery choice.",
                "story_arc": ["Notice the hesitation.", "Give the question a purpose."],
                "tradeoff": "A longer explanation needs space on a small screen.",
            },
            "review_status": "checked",
        }
        self.write()
        self.root_patch = patch.object(art, "ROOT", self.root)
        self.registry_patch = patch.object(art, "REGISTRY_PATH", self.registry)
        self.root_patch.start()
        self.registry_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.registry_patch.stop)

    def write(self, *entries, **top):
        self.registry.write_text(json.dumps({"version": 1, "assets": list(entries or [self.entry]), **top}),
                                 encoding="utf-8")

    def load(self, reference="the-next-question", kind=None):
        return art.load_art_directed(reference, kind or self.entry["kind"], self.policy)

    def pdf(self, pages=2, kind="carousel"):
        path = self.resources / "the-next-question.pdf"
        canvas = Canvas(str(path))
        for _ in range(pages):
            canvas.drawString(30, 30, "A useful question")
            canvas.showPage()
        canvas.save()
        self.entry.update(kind=kind, file="resources/linkedin/stories/the-next-question.pdf",
                          sha256=hashlib.sha256(path.read_bytes()).hexdigest(), page_count=pages)
        self.write()
        return path

    def runner(self):
        return SimpleNamespace(policy=self.policy, state={"actions": {}}, model_paused=True)

    def test_load_returns_exact_reviewed_bytes_without_render_generation_or_state_changes(self):
        runner = self.runner()
        original = self.registry.read_bytes()
        before = deepcopy(runner.state)
        with patch("automation.twin.complete") as model, patch.object(planning, "render_asset") as render:
            asset = planning.prepare_visual(runner, {"format": "image", "art_ref": self.entry["id"]},
                                             "2026-10-15", 18, self.root / "unused")
        model.assert_not_called()
        render.assert_not_called()
        self.assertEqual(asset["path"], str(self.image))
        self.assertEqual(asset["sha256"], self.entry["sha256"])
        self.assertEqual(asset["spec"]["source"], "checked-art-directed")
        self.assertEqual(asset["spec"]["transcript"], self.entry["transcript"])
        self.assertEqual(asset["caption"], self.entry["caption"])
        self.assertIsNone(asset["download_url"])
        self.assertEqual(runner.state, before)
        self.assertEqual(self.registry.read_bytes(), original)
        self.assertFalse((self.root / "unused").exists())

    def test_pdf_validates_actual_pages_and_freebie_link_occurs_once(self):
        self.pdf(kind="freebie")
        asset = self.load()
        expected = art.PUBLIC_RESOURCE_BASE + "linkedin/stories/the-next-question.pdf"
        self.assertEqual(asset["page_count"], 2)
        self.assertEqual(asset["mime_type"], "application/pdf")
        self.assertEqual(asset["download_url"], expected)
        self.assertEqual(asset["caption"].count(expected), 1)
        self.entry["caption"] += "\n\nDownload the worksheet: " + expected
        self.write()
        self.assertEqual(self.load()["caption"].count(expected), 1)

    def test_forged_pdf_count_corruption_and_encryption_are_rejected(self):
        path = self.pdf()
        self.entry["page_count"] = 3
        self.write()
        with self.assertRaisesRegex(VisualAssetError, "page count"):
            self.load()
        path.write_bytes(b"%PDF-1.4\nA header is not a valid document.\n%%EOF")
        self.entry.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), page_count=2)
        self.write()
        with self.assertRaisesRegex(VisualAssetError, "PDF is invalid"):
            self.load()
        self.pdf()
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter()
        writer.append(PdfReader(path))
        writer.encrypt("not-a-secret-test-fixture")
        with path.open("wb") as output:
            writer.write(output)
        self.entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write()
        with self.assertRaisesRegex(VisualAssetError, "PDF is invalid"):
            self.load()

    def test_changed_media_bytes_fail_before_any_remote_write(self):
        Image.new("RGB", (108, 135), "red").save(self.image)
        with patch("automation.twin.requests.post") as publish, self.assertRaisesRegex(VisualAssetError, "checksum"):
            self.load()
        publish.assert_not_called()

    def test_missing_ambiguous_mismatched_and_invalid_references_never_fall_back(self):
        for reference, kind in (("missing", "image"), (None, "image"), ("", "image"),
                                ("../other", "image"), (self.entry["id"], "freebie"),
                                (self.entry["id"], "text")):
            with self.subTest(reference=reference, kind=kind), patch("automation.twin.complete") as model:
                with self.assertRaises(VisualAssetError):
                    self.load(reference, kind)
                model.assert_not_called()
        self.write(self.entry, self.entry)
        with self.assertRaisesRegex(VisualAssetError, "ambiguous"):
            self.load()

    def test_registry_version_and_entry_provenance_are_required(self):
        for mutation in (
            lambda item: item.update(review_status="draft"),
            lambda item: item.pop("transcript"),
            lambda item: item.update(transcript=[]),
            lambda item: item.update(transcript=[""]),
            lambda item: item.update(page_count=True),
            lambda item: item.update(page_count=2),
            lambda item: item.update(sha256="a" * 63),
            lambda item: item.update(download_url="https://example.com"),
            lambda item: item["design_brief"].update(story_arc=[]),
            lambda item: item["design_brief"].pop("tradeoff"),
            lambda item: item["design_brief"].update(visual_idea=""),
        ):
            entry = deepcopy(self.entry)
            mutation(entry)
            self.write(entry)
            with self.subTest(entry=entry), self.assertRaises(VisualAssetError):
                self.load()
        for version in (True, 0, 2, "1"):
            self.write(version=version)
            with self.subTest(version=version), self.assertRaises(VisualAssetError):
                self.load()
        self.registry.write_text("not JSON", encoding="utf-8")
        with self.assertRaisesRegex(VisualAssetError, "could not be read"):
            self.load()

    def test_resource_containment_applies_to_media_and_preview(self):
        for field in ("file", "preview"):
            for path in (str(self.image), "resources/../../outside.png", "resources/./image.png",
                         "https://example.com/card.png", "resources\\outside.png",
                         "resources/missing.png", "automation/registry.png"):
                entry = deepcopy(self.entry)
                entry[field] = path
                self.write(entry)
                with self.subTest(field=field, path=path), self.assertRaises(VisualAssetError):
                    self.load()
        outside = self.root / "outside.png"
        outside.write_bytes(self.image.read_bytes())
        escape = self.resources / "escape.png"
        escape.symlink_to(outside)
        self.entry["preview"] = "resources/linkedin/stories/escape.png"
        self.write()
        with self.assertRaisesRegex(VisualAssetError, "escapes"):
            self.load()

    def test_preview_and_image_content_must_be_valid_not_just_named_png(self):
        self.preview.write_bytes(b"not a PNG")
        with self.assertRaisesRegex(VisualAssetError, "valid PNG"):
            self.load()
        Image.new("RGB", (108, 135), "blue").save(self.preview)
        self.image.write_bytes(b"not an image")
        self.entry["sha256"] = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.write()
        with self.assertRaisesRegex(VisualAssetError, "supported attachment"):
            self.load()

    def test_every_nested_copy_field_has_voice_and_personal_grounding_checks(self):
        for mutation, error in (
            (lambda item: item.update(caption="Choose the next step\u2014then explain it."), VoiceError),
            (lambda item: item.update(title="A seamless checkout"), VoiceError),
            (lambda item: item["transcript"].append("I helped my clients increase conversions."), VisualAssetError),
            (lambda item: item["design_brief"]["story_arc"].append("We tested it with our customers."), VisualAssetError),
            (lambda item: item["design_brief"].update(tradeoff="Use a robust explanation."), VoiceError),
        ):
            entry = deepcopy(self.entry)
            mutation(entry)
            self.write(entry)
            with self.subTest(entry=entry), self.assertRaises(error):
                self.load()
        self.entry["caption"] = "I've worked in product design."
        self.write()
        self.assertEqual(self.load()["caption"], self.entry["caption"])

    def test_all_previous_action_statuses_reserve_authored_media(self):
        runner = self.runner()
        for status in ("scheduled", "published", "rejected", "inflight", "unknown-needs-reconciliation"):
            runner.state["actions"] = {"post:2026-10-12:1800": {
                "kind": "post", "status": status, "asset_id": self.entry["id"]}}
            with self.subTest(status=status), self.assertRaisesRegex(VisualAssetError, "already belongs"):
                planning.prepare_visual(runner, {"format": "image", "art_ref": self.entry["id"]},
                                        "2026-10-15", 18, self.root)

    def test_two_source_selectors_and_text_format_with_art_reference_are_rejected(self):
        for brief in ({"format": "image", "art_ref": self.entry["id"], "media_ref": "old"},
                      {"format": "text", "art_ref": self.entry["id"]}):
            with self.subTest(brief=brief), self.assertRaises(VisualAssetError):
                planning.prepare_visual(self.runner(), brief, "2026-10-15", 18, self.root)

    def test_next_day_queue_schedules_exact_authored_asset_while_model_paused(self):
        policy = {**self.policy, "timezone": "Asia/Calcutta", "platform_id": "linkedin-test",
                  "posting_hours": [18], "posting_days": list(range(7)), "max_posts_per_day": 2,
                  "topics": ["product design"], "max_interactions_per_day": 40}
        runner = Runner(policy, self.root / "state.json", now=datetime(2026, 10, 14, 12, tzinfo=timezone.utc),
                        generate=Mock(), capacity=Mock(return_value=(True, "verified")))
        runner.model_paused = True
        plan = {"slots": [{"date": "2026-10-15", "hour": 18, "format": "image",
                           "topic": "Give a question a reason", "art_ref": self.entry["id"]}]}
        (self.registry.parent / "content-plan.json").write_text(json.dumps(plan), encoding="utf-8")
        with patch("automation.post_queue.ROOT", self.root):
            self.assertEqual(prepare_next_day_posts(runner), ["post:2026-10-15:1800"])
        runner.generate.assert_not_called()
        action = runner.state["actions"]["post:2026-10-15:1800"]
        self.assertEqual(action["asset_id"], self.entry["id"])
        self.assertEqual(action["asset_sha256"], self.entry["sha256"])
        self.assertEqual(action["asset_spec"]["source"], "checked-art-directed")
        self.assertEqual(runner.state["days"]["2026-10-15"]["post"], 1)

    def test_due_slot_also_uses_authored_media_when_model_is_paused(self):
        policy = {**self.policy, "timezone": "Asia/Calcutta", "platform_id": "linkedin-test",
                  "posting_hours": [18], "posting_days": list(range(7)), "max_posts_per_day": 2,
                  "topics": ["product design"], "max_interactions_per_day": 40}
        runner = Runner(policy, self.root / "state.json", now=datetime(2026, 10, 15, 12, 31, tzinfo=timezone.utc),
                        generate=Mock(), capacity=Mock(return_value=(True, "verified")))
        runner.model_paused = True
        plan = {"slots": [{"date": "2026-10-15", "hour": 18, "format": "image",
                           "topic": "Give a question a reason", "art_ref": self.entry["id"]}]}
        (self.registry.parent / "content-plan.json").write_text(json.dumps(plan), encoding="utf-8")
        with patch("automation.twin.ROOT", self.root), patch("automation.twin.skills", return_value="Post brief"):
            runner.post()
        runner.generate.assert_not_called()
        action = runner.state["actions"]["post:2026-10-15:1800"]
        self.assertEqual(action["asset_id"], self.entry["id"])
        self.assertEqual(action["asset_spec"]["source"], "checked-art-directed")


if __name__ == "__main__":
    unittest.main()
