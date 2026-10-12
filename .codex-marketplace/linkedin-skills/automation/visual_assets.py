"""Local, selectable-text LinkedIn documents and image cards.

The source is a reviewed content specification, never a remote image prompt.
All visible copy uses the existing voice boundary. Long copy fails before an
asset is written rather than being clipped or reduced to unreadable type.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from automation.voice import require_plain_text


WIDTH, HEIGHT = 1080, 1350
CREAM = "#F6F3EA"
NAVY = "#142B38"
TEAL = "#136D69"
ORANGE = "#D96837"
WHITE = "#FFFEFA"
MUTED = "#60716F"
LINE = "#D5DCD4"
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_LAYOUTS = {"checklist", "decision", "worksheet"}


class VisualAssetError(ValueError):
    """A content specification cannot be rendered safely and legibly."""


def _copy(value: Any, field: str, limit: int, *, optional: bool = False,
          multiline: bool = False) -> str:
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise VisualAssetError(f"Invalid or overlong {field}")
    text = require_plain_text(value)
    if not multiline and ("\n" in text or "\r" in text):
        raise VisualAssetError(f"Use separate entries for multiline {field}")
    return text


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Return normalized copy without changing words or inventing facts."""
    if not isinstance(spec, dict):
        raise VisualAssetError("The visual specification must be an object")
    slug = spec.get("id")
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise VisualAssetError("Invalid visual id")
    kind = spec.get("kind")
    if kind not in {"image", "carousel", "freebie"}:
        raise VisualAssetError("Unsupported visual kind")
    slides = spec.get("slides")
    if not isinstance(slides, list) or not 1 <= len(slides) <= 11:
        raise VisualAssetError("A visual needs between one and eleven content pages")
    if kind == "image" and len(slides) != 1:
        raise VisualAssetError("An image card needs exactly one content page")
    clean = {
        "id": slug,
        "kind": kind,
        "title": _copy(spec.get("title"), "title", 100),
        "subtitle": _copy(spec.get("subtitle"), "subtitle", 180, optional=True),
        "caption": _copy(spec.get("caption"), "caption", 3000, multiline=True),
        "slides": [],
    }
    for index, slide in enumerate(slides):
        if not isinstance(slide, dict):
            raise VisualAssetError("A slide must be an object")
        layout = slide.get("layout", "worksheet" if kind == "freebie" else "checklist")
        if layout not in _LAYOUTS:
            raise VisualAssetError("Unsupported slide layout")
        body = slide.get("body")
        if not isinstance(body, list) or not 1 <= len(body) <= 4:
            raise VisualAssetError("Each page needs one to four short content entries")
        clean["slides"].append({
            "title": _copy(slide.get("title"), f"slide {index + 1} title", 85),
            "body": [_copy(item, "content entry", 230) for item in body],
            "example": _copy(slide.get("example"), "example", 240, optional=True),
            "layout": layout,
        })
    return clean


def _font_paths() -> tuple[Path, Path]:
    folder = (Path(os.environ["LINKEDIN_VISUAL_FONT_DIR"])
              if os.environ.get("LINKEDIN_VISUAL_FONT_DIR")
              else Path(__file__).resolve().parent / "assets" / "fonts")
    regular, bold = folder / "DejaVuSans.ttf", folder / "DejaVuSans-Bold.ttf"
    if regular.is_file() and bold.is_file():
        return regular, bold
    raise VisualAssetError("The visual font folder needs DejaVu Sans regular and bold")


class _Fonts:
    def __init__(self) -> None:
        from PIL import ImageFont
        self.paths = _font_paths()
        self.loader = ImageFont
        self.cache: dict[tuple[int, bool], Any] = {}

    def get(self, size: int, bold: bool = False) -> Any:
        key = (size, bold)
        if key not in self.cache:
            self.cache[key] = self.loader.truetype(str(self.paths[int(bold)]), size)
        return self.cache[key]

    def wrap(self, text: str, size: int, width: int, *, bold: bool = False,
             max_lines: int = 20) -> list[str]:
        font = self.get(size, bold)
        lines: list[str] = []
        current = ""
        for word in text.split():
            if font.getlength(word) > width:
                raise VisualAssetError("A word is too wide for the visual")
            trial = f"{current} {word}" if current else word
            if font.getlength(trial) > width:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        if len(lines) > max_lines:
            raise VisualAssetError("Visual text would overflow its page")
        return lines


class _Page:
    """One layout for PNG and a true vector PDF with embedded text fonts."""
    def __init__(self, fonts: _Fonts, *, pdf: Any = None) -> None:
        self.fonts, self.pdf = fonts, pdf
        if pdf is None:
            from PIL import Image, ImageDraw
            self.image = Image.new("RGB", (WIDTH, HEIGHT), CREAM)
            self.draw = ImageDraw.Draw(self.image)
        else:
            self.image = None

    def rectangle(self, xy: tuple[int, int, int, int], color: str,
                  *, radius: int = 0, outline: str | None = None) -> None:
        if self.pdf is None:
            self.draw.rounded_rectangle(xy, radius=radius, fill=color, outline=outline, width=2)
            return
        from reportlab.lib.colors import HexColor
        x1, y1, x2, y2 = xy
        self.pdf.setFillColor(HexColor(color))
        if outline:
            self.pdf.setStrokeColor(HexColor(outline))
        self.pdf.roundRect(x1 / 2, (HEIGHT - y2) / 2, (x2 - x1) / 2,
                           (y2 - y1) / 2, radius / 2, fill=1, stroke=int(bool(outline)))

    def line(self, start: tuple[int, int], end: tuple[int, int], color: str,
             width: int = 2) -> None:
        if self.pdf is None:
            self.draw.line((start, end), fill=color, width=width)
            return
        from reportlab.lib.colors import HexColor
        self.pdf.setStrokeColor(HexColor(color))
        self.pdf.setLineWidth(width / 2)
        self.pdf.line(start[0] / 2, (HEIGHT - start[1]) / 2,
                      end[0] / 2, (HEIGHT - end[1]) / 2)

    def text(self, x: int, y: int, text: str, size: int, color: str = NAVY,
             *, bold: bool = False) -> None:
        font = self.fonts.get(size, bold)
        if self.pdf is None:
            self.draw.text((x, y), text, fill=color, font=font, anchor="lt")
            return
        from reportlab.lib.colors import HexColor
        self.pdf.setFillColor(HexColor(color))
        self.pdf.setFont("RupeshSansBold" if bold else "RupeshSans", size / 2)
        # Pillow's glyph-top placement is retained on the vector canvas.
        ascent = font.getmetrics()[0]
        glyph_top = font.getbbox(text)[1]
        self.pdf.drawString(x / 2, (HEIGHT - y - ascent + glyph_top) / 2, text)

    def paragraph(self, x: int, y: int, text: str, size: int, width: int,
                  *, color: str = NAVY, bold: bool = False, max_lines: int = 20,
                  line_height: int | None = None) -> int:
        lines = self.fonts.wrap(text, size, width, bold=bold, max_lines=max_lines)
        step = line_height or round(size * 1.36)
        for index, line in enumerate(lines):
            self.text(x, y + index * step, line, size, color, bold=bold)
        return y + len(lines) * step


def _footer(page: _Page, current: int, total: int, *, dark: bool = False) -> None:
    color = CREAM if dark else NAVY
    page.line((84, 1245), (996, 1245), TEAL if dark else LINE)
    page.text(84, 1276, "Rupesh Kumar", 27, color, bold=True)
    page.text(860, 1276, f"{current:02d} / {total:02d}", 27, color)


def _cover(page: _Page, spec: dict[str, Any], total: int) -> None:
    page.rectangle((0, 0, WIDTH, HEIGHT), NAVY)
    label = "DESIGN NOTES" if spec["kind"] == "carousel" else "WORKING SHEETS"
    page.text(84, 100, label, 27, CREAM, bold=True)
    page.rectangle((84, 174, 150, 185), ORANGE, radius=4)
    # Spare geometric pages suggest an editable working document.
    page.rectangle((853, 86, 977, 218), TEAL, radius=12)
    page.rectangle((833, 67, 957, 199), CREAM, radius=12)
    page.line((857, 102), (925, 102), NAVY, 5)
    page.line((857, 128), (911, 128), TEAL, 5)
    bottom = page.paragraph(84, 350, spec["title"], 78, 912,
                            color=CREAM, bold=True, max_lines=5, line_height=103)
    if spec["subtitle"]:
        bottom = page.paragraph(84, bottom + 60, spec["subtitle"], 38, 830,
                                color=CREAM, max_lines=4, line_height=55)
    if bottom > 1140:
        raise VisualAssetError("The cover text would overflow")
    _footer(page, 1, total, dark=True)


def _content(page: _Page, spec: dict[str, Any], slide: dict[str, Any],
             current: int, total: int, *, image: bool = False) -> None:
    page.rectangle((0, 0, WIDTH, HEIGHT), CREAM)
    page.text(84, 70, "DESIGN NOTES" if spec["kind"] != "freebie" else "WORKING SHEETS",
              25, TEAL, bold=True)
    page.rectangle((84, 127, 144, 137), ORANGE, radius=4)
    title = spec["title"] if image else slide["title"]
    cursor = page.paragraph(84, 181, title, 62, 912, bold=True,
                            max_lines=3, line_height=80) + 45
    if image and spec["subtitle"]:
        cursor = page.paragraph(84, cursor, spec["subtitle"], 30, 912,
                                color=MUTED, max_lines=2, line_height=42) + 28
    layout = slide["layout"]
    for index, item in enumerate(slide["body"]):
        size = 36 if layout == "worksheet" else 38
        lines = page.fonts.wrap(item, size, 764, max_lines=3)
        text_height = len(lines) * 52
        height = text_height + (106 if layout == "worksheet" else 48)
        if cursor + height > 1190:
            raise VisualAssetError("Content entries would overflow the visual")
        page.rectangle((84, cursor, 996, cursor + height), WHITE, radius=20)
        if layout == "decision":
            page.rectangle((110, cursor + 25, 170, cursor + 85), TEAL, radius=14)
            page.text(127, cursor + 38, str(index + 1), 30, WHITE, bold=True)
        elif layout == "worksheet":
            page.rectangle((116, cursor + 30, 144, cursor + 58), WHITE,
                           radius=3, outline=TEAL)
        else:
            page.text(114, cursor + 27, f"{index + 1:02d}", 30, TEAL, bold=True)
        page.paragraph(190, cursor + 25, item, size, 764, max_lines=3, line_height=52)
        if layout == "worksheet":
            page.line((190, cursor + text_height + 61), (946, cursor + text_height + 61), LINE)
            page.line((190, cursor + text_height + 91), (946, cursor + text_height + 91), LINE)
        if layout == "decision" and index < len(slide["body"]) - 1:
            page.line((140, cursor + height), (140, cursor + height + 24), TEAL, 4)
        cursor += height + 20
    if slide["example"]:
        cursor += 14
        example_lines = page.fonts.wrap(slide["example"], 32, 850, max_lines=4)
        height = 32 + len(example_lines) * 44
        if cursor + height > 1190:
            raise VisualAssetError("The example would overflow the visual")
        page.rectangle((84, cursor, 996, cursor + height), "#DFEBE2", radius=16)
        page.paragraph(115, cursor + 18, slide["example"], 32, 850,
                        max_lines=4, line_height=44)
    _footer(page, current, total)


def _draw_pages(spec: dict[str, Any], fonts: _Fonts, pdf: Any = None) -> list[_Page]:
    total = 1 if spec["kind"] == "image" else 1 + len(spec["slides"])
    pages: list[_Page] = []
    if spec["kind"] != "image":
        cover = _Page(fonts, pdf=pdf)
        _cover(cover, spec, total)
        pages.append(cover)
        if pdf is not None:
            pdf.showPage()
    for index, slide in enumerate(spec["slides"]):
        page = _Page(fonts, pdf=pdf)
        _content(page, spec, slide, index + (1 if spec["kind"] == "image" else 2),
                 total, image=spec["kind"] == "image")
        pages.append(page)
        if pdf is not None:
            pdf.showPage()
    return pages


def render_asset(spec: dict[str, Any], output_dir: str | Path) -> dict[str, Any]:
    """Build a PNG or document plus a 1080 x 1350 cover preview.

    A document adds a cover before the supplied content pages. PDF text stays
    selectable and searchable. Rendering has no network or AI subscription.
    File names include a content digest, so later revisions cannot overwrite a
    previously uploaded asset with different content under the same name.
    """
    clean = validate_spec(spec)
    fonts = _Fonts()
    # Build all raster layouts first to check geometry before creating files.
    pages = _draw_pages(clean, fonts)
    digest = hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()[:12]
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{clean['id']}-{digest}"
    preview_path = directory / f"{stem}-preview.png"
    if clean["kind"] == "image":
        path = directory / f"{stem}.png"
        pages[0].image.save(path, format="PNG", optimize=True)
        # One image serves as both its upload and preview.
        preview_path = path
        mime_type = "image/png"
    else:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen.canvas import Canvas
        pdfmetrics.registerFont(TTFont("RupeshSans", str(fonts.paths[0])))
        pdfmetrics.registerFont(TTFont("RupeshSansBold", str(fonts.paths[1])))
        path = directory / f"{stem}.pdf"
        canvas = Canvas(str(path), pagesize=(WIDTH / 2, HEIGHT / 2),
                        pageCompression=1, invariant=1)
        canvas.setTitle(clean["title"])
        canvas.setAuthor("Rupesh Kumar")
        canvas.setSubject("Design notes" if clean["kind"] == "carousel" else "Design working sheets")
        _draw_pages(clean, fonts, canvas)
        canvas.save()
        pages[0].image.save(preview_path, format="PNG", optimize=True)
        mime_type = "application/pdf"
    return {
        "path": str(path.resolve()),
        "preview_path": str(preview_path.resolve()),
        "mime_type": mime_type,
        "title": clean["title"],
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "page_count": len(pages),
    }
