"""Editorial diagrams and selectable-text working documents.

Reviewed specifications describe the actual example or decision on each page.
Typography and geometry are shared by the PNG and vector PDF renderers. Copy
that cannot fit is rejected before any file is written.
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
PAPER = "#F5F3ED"
INK = "#191D23"
BLUE = "#244BDB"
WHITE = "#FFFFFF"
MUTED = "#59616C"
LINE = "#CDD0D3"
PALE = "#E8ECFC"
ACID = "#DFEF8A"
# Public aliases retained for integrations importing the earlier palette.
CREAM, NAVY, TEAL, ORANGE = PAPER, INK, BLUE, ACID
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
_LAYOUTS = {"checklist", "decision", "worksheet", "annotated-ui", "comparison",
            "diagram", "matrix", "framework"}
_RENDER_VERSION = "editorial-plex-3"
_VISUAL_KEYS = {"screens", "annotations", "panels", "nodes", "matrix", "fields"}


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


def _objects(value: Any, field: str, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= limit:
        raise VisualAssetError(f"Invalid {field} count")
    if any(not isinstance(item, dict) for item in value):
        raise VisualAssetError(f"Each {field} entry must be an object")
    return value


def _visual(value: Any, layout: str) -> dict[str, Any]:
    if value is None:
        if layout in {"annotated-ui", "comparison", "diagram", "matrix", "framework"}:
            raise VisualAssetError(f"The {layout} layout needs a structured visual")
        return {}
    if not isinstance(value, dict) or not value or set(value) - _VISUAL_KEYS:
        raise VisualAssetError("Invalid structured visual fields")
    allowed = {
        "annotated-ui": {"screens", "annotations"},
        "comparison": {"panels"}, "diagram": {"nodes", "annotations"},
        "framework": {"nodes", "panels"}, "matrix": {"matrix", "annotations"},
        "worksheet": {"fields", "panels"},
        "checklist": {"panels"}, "decision": {"nodes"},
    }[layout]
    if set(value) - allowed:
        raise VisualAssetError("A visual contains fields that its layout cannot display")
    clean: dict[str, Any] = {}
    for key in ("screens", "annotations", "panels", "nodes", "fields"):
        if key not in value:
            continue
        limit = {"screens": 2, "annotations": 3, "panels": 3,
                 "nodes": 4, "fields": 4}[key]
        items = _objects(value[key], key, limit)
        result = []
        for item in items:
            if key == "screens":
                rows = item.get("rows")
                if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
                    raise VisualAssetError("A screen needs one to four rows")
                entry = {
                    "label": _copy(item.get("label"), "screen label", 30),
                    "title": _copy(item.get("title"), "screen title", 65),
                    "rows": [_copy(row, "screen row", 90) for row in rows],
                    "action": _copy(item.get("action"), "screen action", 35),
                }
            elif key == "annotations":
                entry = {"label": _copy(item.get("label"), "annotation label", 30),
                         "detail": _copy(item.get("detail"), "annotation detail", 100)}
            elif key == "panels":
                body = item.get("body")
                if isinstance(body, list):
                    if not 1 <= len(body) <= 3:
                        raise VisualAssetError("A panel needs one to three body entries")
                    body = [_copy(row, "panel entry", 100) for row in body]
                else:
                    body = _copy(body, "panel body", 180)
                entry = {"label": _copy(item.get("label"), "panel label", 35),
                         "title": _copy(item.get("title"), "panel title", 65),
                         "body": body}
            elif key == "nodes":
                entry = {"label": _copy(item.get("label"), "node label", 35),
                         "title": _copy(item.get("title"), "node title", 65),
                         "detail": _copy(item.get("detail"), "node detail", 110)}
            else:
                entry = {"label": _copy(item.get("label"), "field label", 65),
                         "hint": _copy(item.get("hint"), "field hint", 140)}
            result.append(entry)
        clean[key] = result
    if "matrix" in value:
        matrix = value["matrix"]
        if not isinstance(matrix, dict):
            raise VisualAssetError("A matrix must be an object")
        columns, rows = matrix.get("columns"), matrix.get("rows")
        if not isinstance(columns, list) or not 2 <= len(columns) <= 3:
            raise VisualAssetError("A matrix needs two or three columns")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
            raise VisualAssetError("A matrix needs one to four rows")
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise VisualAssetError("Matrix rows must match the column count")
        clean["matrix"] = {
            "columns": [_copy(column, "matrix heading", 45) for column in columns],
            "rows": [[_copy(cell, "matrix cell", 150) for cell in row] for row in rows],
        }
    required = {"annotated-ui": "screens", "comparison": "panels", "diagram": "nodes",
                "matrix": "matrix"}.get(layout)
    if required and required not in clean:
        raise VisualAssetError(f"The {layout} layout needs {required}")
    if layout == "framework" and not (clean.get("nodes") or clean.get("panels")):
        raise VisualAssetError("A framework needs nodes or panels")
    if layout == "framework" and clean.get("nodes") and clean.get("panels"):
        raise VisualAssetError("Choose one framework structure per page")
    if layout == "worksheet" and "fields" not in clean:
        raise VisualAssetError("A structured worksheet needs writing fields")
    if layout == "worksheet" and len(clean.get("panels", [])) > 1:
        raise VisualAssetError("A worksheet has room for one completed example")
    return clean


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Normalize every visible field without paraphrasing or adding facts."""
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
        "id": slug, "kind": kind,
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
        page = {
            "title": _copy(slide.get("title"), f"slide {index + 1} title", 85),
            "body": [_copy(item, "content entry", 230) for item in body],
            "example": _copy(slide.get("example"), "example", 240, optional=True),
            "layout": layout,
        }
        visual = _visual(slide.get("visual"), layout)
        if visual:
            page["visual"] = visual
        clean["slides"].append(page)
    return clean


def _font_paths() -> dict[str, Path]:
    folder = (Path(os.environ["LINKEDIN_VISUAL_FONT_DIR"])
              if os.environ.get("LINKEDIN_VISUAL_FONT_DIR")
              else Path(__file__).resolve().parent / "assets" / "fonts")
    names = {"sans": "IBMPlexSans-Regular.ttf", "bold": "IBMPlexSans-Medium.ttf",
             "serif": "IBMPlexSerif-Regular.ttf", "mono": "IBMPlexMono-Regular.ttf"}
    paths = {key: folder / name for key, name in names.items()}
    if all(path.is_file() for path in paths.values()):
        return paths
    raise VisualAssetError("The visual font folder needs the four bundled IBM Plex fonts")


class _Fonts:
    def __init__(self) -> None:
        from PIL import ImageFont
        self.paths = _font_paths()
        self.loader = ImageFont
        self.cache: dict[tuple[int, str], Any] = {}

    def get(self, size: int, bold: bool = False, family: str = "sans") -> Any:
        name = "bold" if bold else family
        key = (size, name)
        if key not in self.cache:
            self.cache[key] = self.loader.truetype(str(self.paths[name]), size)
        return self.cache[key]

    def wrap(self, text: str, size: int, width: int, *, bold: bool = False,
             family: str = "sans", max_lines: int = 20) -> list[str]:
        font = self.get(size, bold, family)
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
    """Identical geometry for raster previews and embedded-font vector PDFs."""
    def __init__(self, fonts: _Fonts, *, pdf: Any = None) -> None:
        self.fonts, self.pdf = fonts, pdf
        if pdf is None:
            from PIL import Image, ImageDraw
            self.image = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
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
        self.pdf.setLineWidth(1)
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

    def text(self, x: int, y: int, text: str, size: int, color: str = INK,
             *, bold: bool = False, family: str = "sans") -> None:
        font = self.fonts.get(size, bold, family)
        if self.pdf is None:
            self.draw.text((x, y), text, fill=color, font=font, anchor="lt")
            return
        from reportlab.lib.colors import HexColor
        self.pdf.setFillColor(HexColor(color))
        self.pdf.setFont("RupeshPlex-" + ("bold" if bold else family), size / 2)
        ascent = font.getmetrics()[0]
        glyph_top = font.getbbox(text)[1]
        self.pdf.drawString(x / 2, (HEIGHT - y - ascent + glyph_top) / 2, text)

    def paragraph(self, x: int, y: int, text: str, size: int, width: int,
                  *, color: str = INK, bold: bool = False, family: str = "sans",
                  max_lines: int = 20, line_height: int | None = None) -> int:
        lines = self.fonts.wrap(text, size, width, bold=bold, family=family, max_lines=max_lines)
        step = line_height or round(size * 1.36)
        for index, line in enumerate(lines):
            self.text(x, y + index * step, line, size, color, bold=bold, family=family)
        return y + len(lines) * step

    def height(self, text: str, size: int, width: int, *, family: str = "sans",
               bold: bool = False, max_lines: int = 20, line_height: int | None = None) -> int:
        return len(self.fonts.wrap(text, size, width, family=family, bold=bold,
                                  max_lines=max_lines)) * (line_height or round(size * 1.36))


def _fits(bottom: int) -> None:
    if bottom > 1200:
        raise VisualAssetError("Content would overflow the visual")


def _footer(page: _Page, current: int, total: int, *, dark: bool = False) -> None:
    color = PAPER if dark else INK
    page.line((64, 1246), (1016, 1246), "#6984E8" if dark else LINE)
    page.text(64, 1270, "Rupesh Kumar", 20, color, family="mono")
    page.text(411, 1270, "PRODUCT / BRAND / BUSINESS", 16, color, family="mono")
    page.text(913, 1270, f"{current:02d}/{total:02d}", 20, color, family="mono")


def _cover(page: _Page, spec: dict[str, Any], total: int) -> None:
    freebie = spec["kind"] == "freebie"
    background, foreground = (PAPER, INK) if freebie else (BLUE, PAPER)
    page.rectangle((0, 0, WIDTH, HEIGHT), background)
    page.text(64, 66, "A WORKING DOCUMENT" if freebie else "A DESIGN ARGUMENT", 21,
              foreground, family="mono")
    page.text(921, 66, f"{total:02d} PP", 21, foreground, family="mono")
    page.line((64, 110), (1016, 110), foreground, 2)
    bottom = page.paragraph(64, 179, spec["title"], 84, 920, color=foreground,
                            family="serif", max_lines=4, line_height=96)
    if spec["subtitle"]:
        bottom = page.paragraph(68, bottom + 29, spec["subtitle"], 34, 880,
                                color=foreground, max_lines=4, line_height=46)
    cursor = max(610, bottom + 60)
    if freebie:
        entries = spec["slides"]
        row_heights = []
        for start in range(0, len(entries), 2):
            row_heights.append(max(page.height(item["title"], 22, 373, max_lines=3,
                                               line_height=30)
                                   for item in entries[start:start + 2]) + 33)
        _fits(cursor + sum(row_heights))
        for row_index, start in enumerate(range(0, len(entries), 2)):
            height = row_heights[row_index]
            for column, item in enumerate(entries[start:start + 2]):
                x = 64 + column * 490
                page.line((x, cursor), (x + 460, cursor), INK)
                page.text(x, cursor + 19, f"{start + column + 1:02d}", 21, BLUE, family="mono")
                page.paragraph(x + 64, cursor + 18, item["title"], 22, 373,
                               max_lines=3, line_height=30)
            cursor += height
    else:
        # Use the document's actual trace, so the cover communicates its case.
        trace = next((slide["visual"]["nodes"] for slide in spec["slides"]
                      if slide.get("visual", {}).get("nodes")), None)
        if trace:
            entries = trace[:3]
        else:
            visual = spec["slides"][0].get("visual", {})
            entries = visual.get("panels") or [
                {"label": f"{index + 1:02d}", "title": item["title"], "detail": ""}
                for index, item in enumerate(spec["slides"][:2])]
            entries = entries[:3]
        gap = 30
        width = (952 - gap * (len(entries) - 1)) // len(entries)
        inner = width - 44
        heights = [page.height(item["title"], 37, inner, family="serif", max_lines=4)
                   + page.height(item.get("detail", ""), 24, inner, max_lines=4) + 121
                   for item in entries]
        height = max(310, max(heights))
        hypothetical = "hypothetical" in json.dumps(spec, ensure_ascii=False).lower()
        if hypothetical:
            page.text(65, cursor - 34, "HYPOTHETICAL EXAMPLE", 19, foreground, family="mono")
        _fits(cursor + height)
        for index, item in enumerate(entries):
            x = 64 + index * (width + gap)
            page.rectangle((x, cursor, x + width, cursor + height),
                           ACID if index == 1 or len(entries) == 1 else PAPER)
            y = page.paragraph(x + 22, cursor + 25, item["label"].upper(), 19, inner,
                               color=INK, family="mono", max_lines=2) + 24
            y = page.paragraph(x + 22, y, item["title"], 37, inner, color=INK,
                               family="serif", max_lines=4) + 24
            if item.get("detail"):
                page.paragraph(x + 22, y, item["detail"], 24, inner, color=MUTED, max_lines=4)
            if index < len(entries) - 1:
                mid = cursor + height // 2
                page.line((x + width + 3, mid), (x + width + gap - 4, mid), foreground, 3)
                page.line((x + width + gap - 12, mid - 7),
                          (x + width + gap - 4, mid), foreground, 3)
                page.line((x + width + gap - 12, mid + 7),
                          (x + width + gap - 4, mid), foreground, 3)
    _footer(page, 1, total, dark=not freebie)


def _intro(page: _Page, spec: dict[str, Any], slide: dict[str, Any], image: bool) -> int:
    page.text(64, 65, "DESIGN IN PRACTICE" if image else "WORKING NOTES", 20,
              BLUE, family="mono")
    page.line((64, 111), (1016, 111), INK, 2)
    cursor = page.paragraph(64, 151, spec["title"] if image else slide["title"],
                            72 if image else 64, 952, family="serif",
                            max_lines=3, line_height=86 if image else 78) + 28
    if image and spec["subtitle"]:
        cursor = page.paragraph(67, cursor, spec["subtitle"], 29, 920, color=MUTED,
                                max_lines=3, line_height=39) + 22
    rich = bool(slide.get("visual"))
    if rich:
        for entry in slide["body"]:
            cursor = page.paragraph(67, cursor, entry, 30, 936, max_lines=4,
                                    line_height=41) + 12
        cursor += 16
    return cursor


def _panels(page: _Page, panels: list[dict[str, Any]], cursor: int) -> int:
    width = (952 - 24 * (len(panels) - 1)) // len(panels)
    text_width = width - 56
    sizes = (40, 29) if len(panels) <= 2 else (34, 25)
    title_size, body_size = sizes
    heights = []
    for panel in panels:
        body = panel["body"] if isinstance(panel["body"], list) else [panel["body"]]
        heights.append(107 + page.height(panel["title"], title_size, text_width,
                                        family="serif", max_lines=4)
                       + sum(page.height(text, body_size, text_width, max_lines=7) + 18
                             for text in body) + 28)
    height = max(355, max(heights))
    _fits(cursor + height)
    for index, panel in enumerate(panels):
        x = 64 + index * (width + 24)
        page.rectangle((x, cursor, x + width, cursor + height), WHITE, outline=LINE)
        page.rectangle((x, cursor, x + width, cursor + 8), BLUE if index else INK)
        y = page.paragraph(x + 28, cursor + 31, panel["label"].upper(), 20,
                           text_width, color=BLUE, family="mono", max_lines=2) + 24
        y = page.paragraph(x + 28, y, panel["title"], title_size, text_width,
                           family="serif", max_lines=4) + 26
        for text in panel["body"] if isinstance(panel["body"], list) else [panel["body"]]:
            y = page.paragraph(x + 28, y, text, body_size, text_width,
                               max_lines=7) + 18
        if y > cursor + height - 8:
            raise VisualAssetError("Panel content would overflow")
    return cursor + height


def _nodes(page: _Page, nodes: list[dict[str, Any]], cursor: int) -> int:
    # A vertical evidence chain keeps each relationship legible on a phone.
    heights = [max(117, 29 + max(page.height(node["title"], 34, 405,
                                            family="serif", max_lines=3),
                                page.height(node["detail"], 26, 365, max_lines=4)) + 28)
               for node in nodes]
    _fits(cursor + sum(heights) + 24 * (len(nodes) - 1))
    for index, (node, height) in enumerate(zip(nodes, heights)):
        page.text(65, cursor + 30, f"{index + 1:02d}", 24, BLUE, family="mono")
        page.rectangle((123, cursor, 1016, cursor + height), WHITE, outline=LINE)
        page.paragraph(150, cursor + 23, node["label"].upper(), 18, 405,
                       family="mono", color=BLUE, max_lines=2)
        page.paragraph(150, cursor + 55, node["title"], 34, 405,
                       family="serif", max_lines=3)
        page.line((604, cursor + 25), (604, cursor + height - 25), LINE)
        page.paragraph(627, cursor + 29, node["detail"], 26, 365, max_lines=4)
        cursor += height
        if index < len(nodes) - 1:
            page.line((84, cursor - 17), (84, cursor + 27), BLUE, 2)
            page.line((77, cursor + 20), (84, cursor + 27), BLUE, 2)
            page.line((91, cursor + 20), (84, cursor + 27), BLUE, 2)
            cursor += 24
    return cursor


def _matrix(page: _Page, matrix: dict[str, Any], cursor: int) -> int:
    count = len(matrix["columns"])
    width = 952 // count
    inner = width - 40
    header_height = max(page.height(title, 25, inner, bold=True, max_lines=3)
                        for title in matrix["columns"]) + 40
    row_heights = [max(page.height(cell, 27, inner, max_lines=7) for cell in row) + 28
                   for row in matrix["rows"]]
    _fits(cursor + header_height + sum(row_heights))
    page.rectangle((64, cursor, 1016, cursor + header_height), INK)
    for index, title in enumerate(matrix["columns"]):
        page.paragraph(84 + index * width, cursor + 20, title, 25, inner,
                       color=WHITE, bold=True, max_lines=3)
    cursor += header_height
    for row_index, (row, height) in enumerate(zip(matrix["rows"], row_heights)):
        page.rectangle((64, cursor, 1016, cursor + height), WHITE if row_index % 2 == 0 else PALE)
        for index, cell in enumerate(row):
            page.paragraph(84 + index * width, cursor + 14, cell, 27, inner, max_lines=7)
            if index:
                page.line((64 + index * width, cursor), (64 + index * width, cursor + height), LINE)
        page.line((64, cursor + height), (1016, cursor + height), LINE)
        cursor += height
    return cursor


def _worksheet(page: _Page, visual: dict[str, Any], cursor: int) -> int:
    fields = visual["fields"]
    heights = [page.height(field["label"], 28, 890, bold=True, max_lines=2)
               + page.height(field["hint"], 26, 890, max_lines=3) + 86 for field in fields]
    example = visual.get("panels", [])
    example_height = 0
    if example:
        panel = example[0]
        body = panel["body"] if isinstance(panel["body"], list) else [panel["body"]]
        example_height = (74 + page.height(panel["title"], 33, 866, family="serif", max_lines=3)
                          + sum(page.height(text, 27, 866, max_lines=4) + 10 for text in body) + 26)
    _fits(cursor + sum(heights) + 15 * (len(fields) - 1) + (example_height + 22 if example else 0))
    if example:
        panel = example[0]
        page.rectangle((64, cursor, 1016, cursor + example_height), PALE)
        page.text(90, cursor + 23, panel["label"].upper(), 20, BLUE, family="mono")
        y = page.paragraph(90, cursor + 61, panel["title"], 33, 866,
                           family="serif", max_lines=3) + 16
        for text in panel["body"] if isinstance(panel["body"], list) else [panel["body"]]:
            y = page.paragraph(90, y, text, 27, 866, max_lines=4) + 10
        cursor += example_height + 22
    for index, (field, height) in enumerate(zip(fields, heights)):
        page.line((64, cursor), (1016, cursor), INK)
        y = page.paragraph(64, cursor + 18, field["label"], 28, 890,
                           bold=True, max_lines=2) + 11
        y = page.paragraph(64, y, field["hint"], 26, 890, color=MUTED, max_lines=3)
        page.line((64, y + 33), (1016, y + 33), LINE)
        page.line((64, y + 63), (1016, y + 63), LINE)
        cursor += height + (15 if index < len(fields) - 1 else 0)
    return cursor


def _screens(page: _Page, visual: dict[str, Any], cursor: int) -> int:
    screens = visual["screens"]
    width = (952 - 28 * (len(screens) - 1)) // len(screens)
    inner = width - 48
    heights = []
    for screen in screens:
        title_height = page.height(screen["title"], 30, inner, bold=True, max_lines=3)
        rows_height = sum(page.height(row, 24, inner, max_lines=3) + 19 for row in screen["rows"])
        heights.append(120 + title_height + rows_height + 75)
    height = max(350, max(heights))
    annotations = visual.get("annotations", [])
    annotation_width = (952 - 22 * (len(annotations) - 1)) // max(1, len(annotations))
    annotation_height = max((page.height(item["label"], 20, annotation_width,
                                        family="mono", max_lines=2)
                             + page.height(item["detail"], 25, annotation_width, max_lines=4)
                             + 28 for item in annotations), default=0)
    _fits(cursor + height + (35 + annotation_height if annotations else 0))
    for index, screen in enumerate(screens):
        x = 64 + index * (width + 28)
        page.text(x, cursor, screen["label"].upper(), 20, BLUE, family="mono")
        top, bottom = cursor + 36, cursor + height
        page.rectangle((x, top, x + width, bottom), WHITE, radius=10, outline=LINE)
        page.rectangle((x, top, x + width, top + 33), PALE if index else "#E3E5E8", radius=10)
        page.line((x, top + 33), (x + width, top + 33), LINE)
        for dot in range(3):
            page.rectangle((x + 17 + dot * 13, top + 13, x + 22 + dot * 13, top + 18), MUTED, radius=3)
        y = page.paragraph(x + 24, top + 58, screen["title"], 30, inner,
                           bold=True, max_lines=3) + 29
        for row_index, row in enumerate(screen["rows"]):
            if index and row_index == 0:
                row_height = page.height(row, 24, inner - 24, max_lines=3) + 22
                page.rectangle((x + 14, y - 8, x + width - 14, y + row_height - 8), PALE)
                y = page.paragraph(x + 24, y + 2, row, 24, inner - 24, color=BLUE, max_lines=3) + 19
            else:
                y = page.paragraph(x + 24, y, row, 24, inner, color=MUTED if not index else INK,
                                   max_lines=3) + 19
        button_top = bottom - 65
        if y > button_top - 4:
            raise VisualAssetError("Screen content would overflow")
        page.rectangle((x + 24, button_top, x + width - 24, bottom - 18), BLUE if index else INK, radius=5)
        button_width = page.fonts.get(22, True).getlength(screen["action"])
        if button_width > inner - 20:
            raise VisualAssetError("Screen action would overflow")
        page.text(round(x + width / 2 - button_width / 2), button_top + 13,
                  screen["action"], 22, WHITE, bold=True)
    cursor += height
    if annotations:
        cursor += 35
        for index, item in enumerate(annotations):
            x = 64 + index * (annotation_width + 22)
            page.line((x, cursor), (x + 35, cursor), BLUE, 3)
            y = page.paragraph(x, cursor + 17, item["label"].upper(), 20, annotation_width,
                               family="mono", color=BLUE, max_lines=2) + 14
            page.paragraph(x, y, item["detail"], 25, annotation_width, max_lines=4)
        cursor += annotation_height
    return cursor


def _legacy(page: _Page, slide: dict[str, Any], cursor: int) -> int:
    """Existing concise model specs remain readable without numbered cards."""
    if slide["layout"] == "worksheet":
        fields = [{"label": entry, "hint": ""} for entry in slide["body"]]
        return _worksheet(page, {"fields": fields}, cursor)
    for index, entry in enumerate(slide["body"]):
        height = page.height(entry, 36, 855, max_lines=5) + 43
        _fits(cursor + height)
        page.line((64, cursor), (1016, cursor), INK if index == 0 else LINE)
        page.text(64, cursor + 22, f"{index + 1:02d}", 24, BLUE, family="mono")
        page.paragraph(145, cursor + 20, entry, 36, 855, max_lines=5)
        cursor += height
    return cursor


def _content(page: _Page, spec: dict[str, Any], slide: dict[str, Any],
             current: int, total: int, *, image: bool = False) -> None:
    page.rectangle((0, 0, WIDTH, HEIGHT), PAPER)
    cursor = _intro(page, spec, slide, image)
    visual = slide.get("visual")
    if visual:
        layout = slide["layout"]
        if layout == "annotated-ui":
            cursor = _screens(page, visual, cursor)
        elif layout == "matrix":
            cursor = _matrix(page, visual["matrix"], cursor)
        elif layout == "worksheet":
            cursor = _worksheet(page, visual, cursor)
        elif "panels" in visual:
            cursor = _panels(page, visual["panels"], cursor)
        else:
            cursor = _nodes(page, visual["nodes"], cursor)
    else:
        cursor = _legacy(page, slide, cursor)
    if slide["example"]:
        cursor += 26
        height = page.height(slide["example"], 28, 896, max_lines=5) + 45
        _fits(cursor + height)
        page.rectangle((64, cursor, 1016, cursor + height), PALE)
        page.paragraph(92, cursor + 22, slide["example"], 28, 896, max_lines=5)
    if visual and slide["layout"] != "annotated-ui" and visual.get("annotations"):
        cursor += 30
        annotations = visual["annotations"]
        width = (952 - 22 * (len(annotations) - 1)) // len(annotations)
        height = max(page.height(item["label"], 20, width, family="mono", max_lines=2)
                     + page.height(item["detail"], 25, width, max_lines=4) + 31
                     for item in annotations)
        _fits(cursor + height)
        for index, item in enumerate(annotations):
            x = 64 + index * (width + 22)
            page.line((x, cursor), (x + 35, cursor), BLUE, 3)
            y = page.paragraph(x, cursor + 17, item["label"].upper(), 20, width,
                               family="mono", color=BLUE, max_lines=2) + 14
            page.paragraph(x, y, item["detail"], 25, width, max_lines=4)
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
    """Render a PNG or document and a matching full-size cover preview.

    PDFs contain selectable text and embedded licensed fonts. A renderer version
    participates in the content digest so a design revision cannot silently
    overwrite bytes associated with an earlier scheduled upload.
    """
    clean = validate_spec(spec)
    fonts = _Fonts()
    pages = _draw_pages(clean, fonts)
    digest_source = {"renderer": _RENDER_VERSION, "spec": clean}
    digest = hashlib.sha256(json.dumps(digest_source, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()[:12]
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{clean['id']}-{digest}"
    preview_path = directory / f"{stem}-preview.png"
    if clean["kind"] == "image":
        path = directory / f"{stem}.png"
        pages[0].image.save(path, format="PNG", optimize=True)
        preview_path = path
        mime_type = "image/png"
    else:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen.canvas import Canvas
        for family, font_path in fonts.paths.items():
            pdfmetrics.registerFont(TTFont("RupeshPlex-" + family, str(font_path)))
        path = directory / f"{stem}.pdf"
        canvas = Canvas(str(path), pagesize=(WIDTH / 2, HEIGHT / 2),
                        pageCompression=1, invariant=1)
        canvas.setTitle(clean["title"])
        canvas.setAuthor("Rupesh Kumar")
        canvas.setSubject("Product and brand design decisions" if clean["kind"] == "carousel"
                          else "Design decision worksheets")
        _draw_pages(clean, fonts, canvas)
        canvas.save()
        pages[0].image.save(preview_path, format="PNG", optimize=True)
        mime_type = "application/pdf"
    return {"path": str(path.resolve()), "preview_path": str(preview_path.resolve()),
            "mime_type": mime_type, "title": clean["title"],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "page_count": len(pages)}
