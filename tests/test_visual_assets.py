"""Visual assets stay readable, factual-source-bound, and usable as documents."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from PIL import Image

from automation.visual_assets import VisualAssetError, render_asset, validate_spec
from automation.voice import VoiceError


SPEC = {
    "id": "review-before-pixels",
    "kind": "image",
    "title": "Before changing the pixels",
    "subtitle": "Start with the task someone needs to finish.",
    "caption": "A screen review gets easier when the team agrees what the person needs to do.\n\nWrite the task before opening the design file.",
    "slides": [{
        "title": "Ask about the task",
        "body": ["What is the person trying to finish?",
                 "Which action matters most on this screen?",
                 "What happens when the screen is empty?"],
    }],
}


class VisualAssetTests(unittest.TestCase):
    def test_every_scheduled_catalog_asset_fits_and_produces_its_promised_format(self):
        catalog_path = Path(__file__).resolve().parents[1] / "automation" / "visual-content.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            for spec in catalog["assets"]:
                with self.subTest(asset=spec["id"]):
                    result = render_asset(spec, directory)
                    expected = "image/png" if spec["kind"] == "image" else "application/pdf"
                    self.assertEqual(result["mime_type"], expected)
                    self.assertTrue(Path(result["path"]).is_file())

    def test_image_is_full_size_and_keeps_proof_of_uploaded_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            result = render_asset(SPEC, directory)
            with Image.open(result["path"]) as image:
                self.assertEqual(image.size, (1080, 1350))
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.mode, "RGB")
            self.assertEqual(result["preview_path"], result["path"])
            self.assertEqual(result["mime_type"], "image/png")
            self.assertEqual(result["page_count"], 1)
            self.assertEqual(result["sha256"],
                             hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest())

    @unittest.skipUnless(shutil.which("pdftotext"), "PDF text extraction needs pdftotext")
    def test_freebie_pdf_contains_selectable_copy_and_all_content_pages(self):
        spec = deepcopy(SPEC)
        spec["kind"] = "freebie"
        spec["slides"].append({"title": "Choose what to watch", "layout": "decision",
                               "body": ["Keep the customer task the same for both versions."]})
        with tempfile.TemporaryDirectory() as directory:
            result = render_asset(spec, directory)
            extracted = subprocess.run(["pdftotext", "-layout", result["path"], "-"],
                                       check=True, capture_output=True, text=True).stdout
            self.assertIn(spec["title"], " ".join(extracted.split()))
            for slide in spec["slides"]:
                self.assertIn(slide["title"], extracted)
                for entry in slide["body"]:
                    self.assertIn(entry, " ".join(extracted.split()))
            self.assertEqual(result["page_count"], 3)
            self.assertEqual(len([page for page in extracted.split("\f") if page.strip()]), 3)
            self.assertIn("Rupesh Kumar", extracted)
            self.assertEqual(result["mime_type"], "application/pdf")
            self.assertTrue(Path(result["preview_path"]).is_file())

    def test_every_copy_field_is_checked_before_any_file_exists(self):
        for location in ("title", "subtitle", "caption", "slide-title", "body", "example"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as directory:
                spec = deepcopy(SPEC)
                bad = "Test first \u2014 then ship."
                if location in {"title", "subtitle", "caption"}:
                    spec[location] = bad
                elif location == "slide-title":
                    spec["slides"][0]["title"] = bad
                elif location == "body":
                    spec["slides"][0]["body"][0] = bad
                else:
                    spec["slides"][0]["example"] = bad
                with self.assertRaises(VoiceError):
                    render_asset(spec, directory)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_overflow_is_rejected_before_writing_and_does_not_shrink_copy(self):
        spec = deepcopy(SPEC)
        # All text is within character limits; the geometric limit is what fails.
        spec["title"] = "Review " * 13
        spec["subtitle"] = "Keep the same customer task in each version. " * 4
        spec["slides"][0]["body"] = ["Ask someone to try the checkout step without help. " * 4] * 4
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(VisualAssetError, "overflow"):
                render_asset(spec, directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_revisions_get_distinct_files_and_documents_are_repeatable(self):
        spec = deepcopy(SPEC)
        spec["kind"] = "carousel"
        with tempfile.TemporaryDirectory() as directory:
            first = render_asset(spec, directory)
            repeat = render_asset(spec, directory)
            self.assertEqual(first["sha256"], repeat["sha256"])
            self.assertEqual(first["path"], repeat["path"])
            spec["slides"][0]["body"][0] = "What should someone be able to finish?"
            revised = render_asset(spec, directory)
            self.assertNotEqual(first["path"], revised["path"])
            self.assertTrue(Path(first["path"]).is_file())

    def test_unsafe_ids_unsupported_layouts_and_multiple_image_pages_fail(self):
        for change in ({"id": "../outside"}, {"kind": "video"}, {"id": ""}):
            with self.subTest(change=change):
                with self.assertRaises(VisualAssetError):
                    validate_spec({**SPEC, **change})
        spec = deepcopy(SPEC)
        spec["slides"][0]["layout"] = "unbounded"
        with self.assertRaises(VisualAssetError):
            validate_spec(spec)
        spec = deepcopy(SPEC)
        spec["slides"].append(deepcopy(spec["slides"][0]))
        with self.assertRaises(VisualAssetError):
            validate_spec(spec)


if __name__ == "__main__":
    unittest.main()
