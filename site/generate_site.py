#!/usr/bin/env python3
"""Generate the GitHub Pages site for a plugin repo from its README.

Each plugin repo already documents itself completely in README.md, with the
screenshots it references committed alongside it. This renders that README into
a designed product page — hero, screenshot gallery, the full control manual and
a collapsible release history — and writes it to <plugin>/docs/, which is what
GitHub Pages serves ("Deploy from a branch", main, /docs).

The README stays the single source of truth: never hand-edit docs/, it is
overwritten on the next run. Run it through the workflow CLI:

    ./dmse site omni-84
    ./dmse site all

Screenshots are re-encoded for the web (max 1200 px wide, WebP when cwebp is
installed, otherwise JPEG via sips/ImageMagick/Pillow) so a page weighs a few
hundred KB instead of the 12 MB the raw PNGs would cost.

Optional per-plugin overrides live in <plugin>/site.json (all keys optional):

    {
      "storeUrl": "https://store.dehlimusikk.no/l/omni-84",
      "tagline":  "One-line pitch, overrides the README's first sentence",
      "heroImage": "Screenshots/Keyboard.png"
    }

Only the Python standard library is used, so it runs on a stock macOS.
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import html
import json
import re
import shutil
import struct
import subprocess
import sys
import urllib.parse
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent
ROOT = SITE_DIR.parent

BRAND = "Dehli Musikk"
BRAND_URL = "https://www.dehlimusikk.no/"

# Stable identities for the structured data. The author is keyed by MusicBrainz
# so the same person resolves across this site, the store and the record labels'
# data; the organisation is keyed by its own home page.
#
# Both are deliberately published here as stubs: @id, type and name, and nothing
# more. dehlimusikk.no describes the same two entities in full at the same @ids,
# addresses, profiles, logo, founding date and the rest, so a crawler that
# reconciles by @id already has them. Restating any of it on thirteen product
# sites only creates a second copy to keep current, and the copies do fall
# behind. Per-product profiles are a different matter and live in
# plugin-data.json under "sameAs", because no other page states them.
AUTHOR_ID = "https://musicbrainz.org/artist/56639e59-2bb5-40bd-9d5a-97d964298b6f"
AUTHOR_NAME = "Benjamin Dehli"
PUBLISHER_ID = BRAND_URL
# The pages are generated from each repository's README and are published from
# that same repository, so they carry the repository's licence. GPL 3.0 only:
# neither the LICENSE files nor the engine README offer the "or any later
# version" option.
CONTENT_LICENSE = "https://www.gnu.org/licenses/gpl-3.0.html"
DEFAULT_STORE_URL = "https://store.dehlimusikk.no/"
DECENT_SAMPLER_URL = "https://www.decentsamples.com/product/decent-sampler-plugin/"

MAX_IMAGE_WIDTH = 1200
THUMB_WIDTH = 400
PHOTO_QUALITY = 80
ICON_SIZE = 256

# Store links, sameAs profiles and demo videos for every product, shared by all
# pages. Per-plugin site.json still wins over anything in here.
PLUGIN_DATA = SITE_DIR / "plugin-data.json"
YOUTUBE_ID_RE = re.compile(r"(?:v=|/shorts/|youtu\.be/|/embed/)([A-Za-z0-9_-]{6,})")

# Screenshots at or below this width are control close-ups, not full-GUI shots:
# shown at their natural size instead of stretched across the column.
NARROW_SHOT_WIDTH = 700

INTRO_SECTIONS = ("introduction", "description")
RELEASE_SECTION = "release notes"
ABOUT_SECTION = "about this repository"
FORMATS_SECTION = "included formats"
SPEC_SECTION = "technical specification"


# ── small helpers ────────────────────────────────────────────────────────────

def info(msg: str) -> None:
    print(f"\033[1m==>\033[0m {msg}" if sys.stdout.isatty() else f"==> {msg}")


def warn(msg: str) -> None:
    print(f"\033[33m!!\033[0m {msg}" if sys.stderr.isatty() else f"!! {msg}", file=sys.stderr)


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"Error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", strip_markdown(text).lower()).strip("-")
    return slug or "section"


class Slugger:
    """Hands out unique ids — READMEs reuse titles (## Effects, ### Effects)."""

    def __init__(self):
        self.seen = {}

    def __call__(self, text: str) -> str:
        base = slugify(text)
        count = self.seen.get(base, 0)
        self.seen[base] = count + 1
        return base if count == 0 else f"{base}-{count + 1}"


def strip_markdown(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"[*`]", "", text)
    return text.strip()


def run(cmd: list) -> bool:
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


# ── image size probing (PNG / JPEG, stdlib only) ─────────────────────────────

def image_size(path: Path):
    """(width, height) for a PNG or JPEG, or None if it can't be determined."""
    try:
        with path.open("rb") as fh:
            head = fh.read(26)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
            if head[:2] != b"\xff\xd8":
                return None
            fh.seek(2)
            while True:
                marker = fh.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                size = struct.unpack(">H", fh.read(2))[0]
                # SOF0..SOF15, excluding the non-frame markers DHT/JPG/DAC.
                if 0xC0 <= marker[1] <= 0xCF and marker[1] not in (0xC4, 0xC8, 0xCC):
                    data = fh.read(5)
                    height, width = struct.unpack(">HH", data[1:5])
                    return width, height
                fh.seek(size - 2, 1)
    except OSError:
        return None


def scaled_size(size, max_width: int):
    if not size:
        return None
    width, height = size
    if width <= max_width:
        return width, height
    return max_width, max(1, round(height * max_width / width))


# ── image encoding ───────────────────────────────────────────────────────────

def have_pillow() -> bool:
    try:
        import PIL.Image  # noqa: F401
        return True
    except ImportError:
        return False


class Encoder:
    """Re-encodes screenshots for the web with whatever tool the machine has.

    Preference order is deliberate: cwebp gives the smallest files, sips is
    always present on macOS, ImageMagick and Pillow cover Linux dev boxes.
    """

    def __init__(self, want_format: str = "auto"):
        self.pillow = have_pillow()
        self.magick = shutil.which("magick") or shutil.which("convert")
        self.sips = shutil.which("sips")
        self.cwebp = shutil.which("cwebp")

        if want_format == "auto":
            want_format = "webp" if (self.cwebp or self.pillow) else "jpeg"
        if want_format == "webp" and not (self.cwebp or self.pillow or self.magick):
            warn("no WebP encoder found — falling back to JPEG")
            want_format = "jpeg"
        self.format = want_format
        self.ext = "webp" if want_format == "webp" else "jpg"

        self.tool = self._pick_tool()

    def _pick_tool(self):
        if self.format == "webp":
            for tool in ("cwebp", "pillow", "magick"):
                if getattr(self, tool, None):
                    return tool
            return None
        for tool in ("sips", "magick", "pillow"):
            if getattr(self, tool, None):
                return tool
        return None

    def describe(self) -> str:
        return f"{self.format} via {self.tool}" if self.tool else "unoptimized copies"

    def photo(self, src: Path, dst: Path, max_width: int) -> bool:
        """Encode src into dst, downscaling to max_width. False if copied as-is."""
        size = image_size(src)
        needs_resize = bool(size and size[0] > max_width)
        width = max_width if needs_resize else (size[0] if size else max_width)

        if self.tool == "cwebp":
            cmd = ["cwebp", "-quiet", "-q", str(PHOTO_QUALITY)]
            if needs_resize:
                cmd += ["-resize", str(width), "0"]
            if run(cmd + [str(src), "-o", str(dst)]):
                return True
        elif self.tool == "sips":
            cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(PHOTO_QUALITY)]
            if needs_resize:
                cmd += ["-Z", str(width)]
            if run(cmd + [str(src), "--out", str(dst)]):
                return True
        elif self.tool == "magick":
            cmd = [self.magick, str(src)]
            if needs_resize:
                cmd += ["-resize", f"{width}x>"]
            cmd += ["-quality", str(PHOTO_QUALITY), str(dst)]
            if run(cmd):
                return True
        elif self.tool == "pillow":
            if self._pillow_save(src, dst, width if needs_resize else None, keep_alpha=False):
                return True

        shutil.copy2(src, dst)
        return False

    def icon(self, src: Path, dst: Path) -> bool:
        """Resize the app icon to ICON_SIZE, keeping its alpha channel (PNG)."""
        if self.sips and run(
            ["sips", "-s", "format", "png", "-Z", str(ICON_SIZE), str(src), "--out", str(dst)]
        ):
            return True
        if self.magick and run([self.magick, str(src), "-resize", f"{ICON_SIZE}x{ICON_SIZE}>", str(dst)]):
            return True
        if self.pillow and self._pillow_save(src, dst, ICON_SIZE, keep_alpha=True):
            return True
        shutil.copy2(src, dst)
        return False

    def _pillow_save(self, src: Path, dst: Path, width, keep_alpha: bool) -> bool:
        try:
            from PIL import Image

            with Image.open(src) as im:
                if width and im.width > width:
                    im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
                if keep_alpha:
                    im.save(dst)
                else:
                    if im.mode in ("RGBA", "LA", "P"):
                        im = im.convert("RGB")
                    im.save(dst, quality=PHOTO_QUALITY, optimize=True)
            return True
        except Exception as exc:  # pragma: no cover - depends on local Pillow
            warn(f"Pillow failed on {src.name}: {exc}")
            return False


class Images:
    """Registers every image the page references and emits optimized copies."""

    def __init__(self, plugin_dir: Path, out_dir: Path, encoder: Encoder):
        self.plugin_dir = plugin_dir
        self.out_dir = out_dir
        self.encoder = encoder
        # by_src maps every lookup key (README path or file path) to its entry;
        # entries holds each image once, since two keys can share one entry.
        self.by_src = {}
        self.entries = []
        self.written = set()

    def register(self, src: str):
        """Map a README image path to the emitted copy. None if it's missing."""
        key = src.strip()
        if key in self.by_src:
            return self.by_src[key]
        if re.match(r"^[a-z]+://", key):
            return None  # remote image: left untouched by render_inline

        source = self.plugin_dir / key.lstrip("/")
        if not source.is_file():
            warn(f"{self.plugin_dir.name}: README references a missing image: {src}")
            self.by_src[key] = None
            return None

        entry = self.register_file(source, f"{source.parent.name}-{source.stem}")
        self.by_src[key] = entry
        return entry

    def register_file(self, source: Path, name: str, max_width: int = MAX_IMAGE_WIDTH):
        """Register any file on disk, including one from a sibling plugin repo."""
        key = f"{source}@{max_width}"
        if key in self.by_src:
            return self.by_src[key]

        stem = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        # Without an encoder the file is copied verbatim, so keep its real type.
        ext = self.encoder.ext if self.encoder.tool else source.suffix.lstrip(".").lower()
        entry = {
            "url": f"img/{stem}.{ext}",
            "path": self.out_dir / "img" / f"{stem}.{ext}",
            "source": source,
            "max_width": max_width,
            # Without an encoder nothing is downscaled, so don't claim it was.
            "size": scaled_size(image_size(source), max_width)
            if self.encoder.tool
            else image_size(source),
        }
        self.by_src[key] = entry
        self.entries.append(entry)
        return entry

    def emit(self) -> None:
        img_dir = self.out_dir / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        for entry in self.entries:
            encoded = self.encoder.photo(
                entry["source"], entry["path"], entry.get("max_width", MAX_IMAGE_WIDTH)
            )
            self.written.add(entry["path"].name)
            # A copy-as-is fallback is not downscaled, so re-read what was written.
            if not encoded or not entry["size"]:
                entry["size"] = image_size(entry["path"])

    def prune(self) -> None:
        img_dir = self.out_dir / "img"
        if not img_dir.is_dir():
            return
        for stale in img_dir.iterdir():
            if stale.is_file() and stale.name not in self.written:
                stale.unlink()


# ── markdown parsing ─────────────────────────────────────────────────────────

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
LIST_RE = re.compile(r"^(\s*)-\s+(.*)$")
REFDEF_RE = re.compile(r"^\[([^\]]+)\]:\s*(\S+)\s*$")
SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
REFLINK_RE = re.compile(r"\[([^\]]+)\]\[([^\]]*)\]")
CODE_RE = re.compile(r"`([^`]+)`")
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
EM_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
VERSION_RE = re.compile(r"^Version\s+([0-9][0-9.]*)\s*(?:\(([^)]+)\))?", re.I)


def split_row(line: str):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_markdown(text: str):
    """Return (blocks, reference-link definitions) for the README subset used."""
    refs = {}
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        match = REFDEF_RE.match(line)
        if match:
            refs[match.group(1).lower()] = match.group(2)
        else:
            lines.append(line)

    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        heading = HEADING_RE.match(line)
        if heading:
            blocks.append(
                {"type": "heading", "level": len(heading.group(1)), "text": heading.group(2)}
            )
            i += 1
            continue

        if line.lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            blocks.append(make_table(rows))
            continue

        if LIST_RE.match(line):
            items, i = parse_list(lines, i)
            blocks.append({"type": "list", "items": items})
            continue

        para = []
        while i < len(lines) and lines[i].strip():
            if HEADING_RE.match(lines[i]) or lines[i].lstrip().startswith("|") or LIST_RE.match(lines[i]):
                break
            para.append(lines[i].strip())
            i += 1
        blocks.append({"type": "para", "text": " ".join(para)})

    return blocks, refs


def parse_list(lines, i: int):
    items = []
    stack = [(-1, items)]
    while i < len(lines):
        match = LIST_RE.match(lines[i])
        if not match:
            break
        indent = len(match.group(1).expandtabs(4))
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        node = {"text": match.group(2), "children": []}
        stack[-1][1].append(node)
        stack.append((indent, node["children"]))
        i += 1
    return items, i


def make_table(rows):
    """Classify a pipe table: caption figure, equipment gallery, or real table."""
    sep_index = next(
        (n for n, row in enumerate(rows) if row and all(SEP_CELL_RE.match(c) for c in row)),
        None,
    )
    if sep_index is None:
        header, align, body = None, [], rows
    else:
        header = rows[0] if sep_index == 1 else None
        align = [cell_align(c) for c in rows[sep_index]]
        body = rows[sep_index + 1:]

    # |![alt](img)| / |:--:| / |caption|  — the README idiom for a captioned figure.
    if header and len(header) == 1 and IMG_RE.fullmatch(header[0]):
        match = IMG_RE.fullmatch(header[0])
        caption = body[0][0] if body and body[0] else match.group(1)
        return {"type": "figure", "src": match.group(2), "alt": match.group(1), "caption": caption}

    # Several images side by side with a row of captions under them (EDB-Orgel's
    # tabs). As a table this is far wider than the column; as figures it isn't.
    if header and len(header) > 1 and all(IMG_RE.fullmatch(c) for c in header):
        captions = body[0] if body else []
        figures = []
        for n, cell in enumerate(header):
            match = IMG_RE.fullmatch(cell)
            figures.append({
                "src": match.group(2),
                "alt": match.group(1),
                "caption": captions[n] if n < len(captions) else match.group(1),
            })
        return {"type": "figure-row", "items": figures}

    # "Equipment used": a name column and an image column.
    if body and any(IMG_RE.search(cell) for row in body for cell in row):
        items = []
        for row in body:
            image = next((IMG_RE.search(c) for c in row if IMG_RE.search(c)), None)
            label = next((c for c in row if not IMG_RE.search(c)), "")
            if image:
                items.append({"src": image.group(2), "alt": image.group(1), "label": label})
        if items:
            return {"type": "gear", "items": items}

    return {"type": "table", "header": header, "align": align, "rows": body}


def cell_align(cell: str) -> str:
    left, right = cell.startswith(":"), cell.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    return "left"


# ── rendering ────────────────────────────────────────────────────────────────

class Renderer:
    def __init__(self, refs, images: Images, skip_images=()):
        self.refs = refs
        self.images = images
        self.skip_images = set(skip_images)

    # -- inline ------------------------------------------------------------
    def inline(self, text: str) -> str:
        out = html.escape(text, quote=False)
        out = CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
        out = IMG_RE.sub(self._inline_image, out)
        out = LINK_RE.sub(lambda m: self._link(m.group(1), m.group(2)), out)
        out = REFLINK_RE.sub(self._ref_link, out)
        out = BOLD_RE.sub(r"<strong>\1</strong>", out)
        out = EM_RE.sub(r"<em>\1</em>", out)
        return out

    def _link(self, label: str, href: str) -> str:
        external = href.startswith("http")
        rel = ' target="_blank" rel="noopener"' if external else ""
        return f'<a href="{html.escape(href, quote=True)}"{rel}>{label}</a>'

    def _ref_link(self, match) -> str:
        label, ref = match.group(1), (match.group(2) or match.group(1))
        href = self.refs.get(ref.lower())
        return self._link(label, href) if href else label

    def _inline_image(self, match) -> str:
        entry = self.images.register(match.group(2))
        alt = html.escape(match.group(1), quote=True)
        if not entry:
            return alt
        return f'<img src="{entry["url"]}" alt="{alt}" loading="lazy">'

    # -- blocks ------------------------------------------------------------
    def blocks(self, blocks) -> str:
        return "\n".join(part for part in (self.block(b) for b in blocks) if part)

    def block(self, block) -> str:
        kind = block["type"]
        if kind == "heading":
            level = min(block["level"], 6)
            slug = block.get("slug") or slugify(block["text"])
            anchor = f'<a class="anchor" href="#{slug}" aria-hidden="true">#</a>' if level <= 3 else ""
            return f'<h{level} id="{slug}">{anchor}{self.inline(block["text"])}</h{level}>'

        if kind == "para":
            # A paragraph that is nothing but an image is really a figure.
            match = IMG_RE.fullmatch(block["text"].strip())
            if match:
                return self.figure(match.group(2), match.group(1), match.group(1))
            return f'<p>{self.inline(block["text"])}</p>'

        if kind == "list":
            return self.list(block["items"])

        if kind == "figure":
            return self.figure(block["src"], block["alt"], block["caption"])

        if kind == "figure-row":
            # A row is a set — dropping the one that happens to be the hero image
            # would leave the reader comparing two of three tabs.
            figures = [
                self.figure(i["src"], i["alt"], i["caption"], allow_skip=False)
                for i in block["items"]
            ]
            figures = [f for f in figures if f]
            if not figures:
                return ""
            return f'<div class="figure-row">{"".join(figures)}</div>'

        if kind == "gear":
            return self.gear(block["items"])

        if kind == "table":
            return self.table(block)

        return ""

    def list(self, items) -> str:
        out = ["<ul>"]
        for item in items:
            child = self.list(item["children"]) if item["children"] else ""
            out.append(f'<li>{self.inline(item["text"])}{child}</li>')
        out.append("</ul>")
        return "\n".join(out)

    def figure(self, src: str, alt: str, caption: str, allow_skip: bool = True) -> str:
        entry = self.images.register(src)
        if not entry:
            return f"<p>{self.inline(caption or alt)}</p>"
        if allow_skip and src.strip() in self.skip_images:
            return ""  # already shown as the hero image
        size = entry["size"] or (0, 0)
        dims = f' width="{size[0]}" height="{size[1]}"' if size[0] else ""
        narrow = " shot-narrow" if size[0] and size[0] <= NARROW_SHOT_WIDTH else ""
        caption_html = f"<figcaption>{self.inline(caption)}</figcaption>" if caption else ""
        return (
            f'<figure class="shot{narrow}">'
            f'<a href="{entry["url"]}" target="_blank" rel="noopener">'
            f'<img src="{entry["url"]}" alt="{html.escape(alt, quote=True)}" loading="lazy"{dims}>'
            f"</a>{caption_html}</figure>"
        )

    def gear(self, items) -> str:
        out = ['<ul class="gear">']
        for item in items:
            entry = self.images.register(item["src"])
            label = self.inline(item["label"]) if item["label"] else self.inline(item["alt"])
            image = ""
            if entry:
                image = (
                    f'<img src="{entry["url"]}" alt="{html.escape(item["alt"], quote=True)}" loading="lazy">'
                )
            out.append(f'<li>{image}<span class="label">{label}</span></li>')
        out.append("</ul>")
        return "\n".join(out)

    def table(self, block) -> str:
        align = block["align"]

        def cells(row, tag):
            out = []
            for n, cell in enumerate(row):
                style = f' style="text-align:{align[n]}"' if n < len(align) and align[n] != "left" else ""
                out.append(f"<{tag}{style}>{self.inline(cell)}</{tag}>")
            return "".join(out)

        parts = ['<div class="table-wrap"><table>']
        if block["header"]:
            parts.append(f"<thead><tr>{cells(block['header'], 'th')}</tr></thead>")
        parts.append("<tbody>")
        for row in block["rows"]:
            parts.append(f"<tr>{cells(row, 'td')}</tr>")
        parts.append("</tbody></table></div>")
        return "".join(parts)


# ── document structure ───────────────────────────────────────────────────────

def organize(blocks):
    """Split the README into a title, intro blocks and h2 sections."""
    title = ""
    start = 0
    for n, block in enumerate(blocks):
        if block["type"] == "heading" and block["level"] == 1:
            title = block["text"]
            start = n + 1
            break

    intro, sections, current = [], [], None
    for block in blocks[start:]:
        if block["type"] == "heading" and block["level"] == 2:
            current = {"title": block["text"], "blocks": []}
            sections.append(current)
            continue
        (current["blocks"] if current else intro).append(block)
    return title, intro, sections


def assign_slugs(sections) -> None:
    """Give every section and sub-heading a unique id, once, up front."""
    slugger = Slugger()
    for section in sections:
        section["slug"] = slugger(section["title"])
        for block in section["blocks"]:
            if block["type"] == "heading":
                block["slug"] = slugger(block["text"])


def take_section(sections, name: str):
    for n, section in enumerate(sections):
        if section["title"].strip().lower() == name:
            return sections.pop(n)
    return None


def find_section(sections, name: str):
    return next((s for s in sections if s["title"].strip().lower() == name), None)


def latest_release(section):
    """(version, date) from the first '### Version x.y.z (date)' heading."""
    if not section:
        return None, None
    for block in section["blocks"]:
        if block["type"] == "heading" and block["level"] == 3:
            match = VERSION_RE.match(strip_markdown(block["text"]))
            if match:
                return match.group(1), match.group(2)
    return None, None


def format_details(section):
    """(format names, operating systems, per-format entries) from 'Included formats'.

    The list reads "VST3 (macOS)", "Decent Sampler (macOS, Windows and Linux)",
    which gives the badges, an accurate schema.org operatingSystem, and — kept
    per entry rather than flattened — which platform each format actually runs
    on, so the FAQ can say the plugin is macOS only without guessing.
    """
    if not section:
        return [], [], []
    names, systems, entries = [], [], []
    for block in section["blocks"]:
        if block["type"] != "list":
            continue
        for item in block["items"]:
            raw = strip_markdown(item["text"])
            name = raw.split(" (")[0].replace(" application", "").strip()
            if name and name not in names:
                names.append(name)
            own = []
            inside = re.search(r"\(([^)]+)\)", raw)
            if inside:
                for system in re.split(r",|\band\b", inside.group(1)):
                    system = system.strip()
                    if not system:
                        continue
                    own.append(system)
                    if system not in systems:
                        systems.append(system)
            if name:
                entries.append({"name": name, "systems": own})
    return names, systems, entries


DECENT_SAMPLER_NAME = "decent sampler"


def join_words(words) -> str:
    """"VST3, AU and Standalone" — a list as a reader would say it out loud."""
    words = [w for w in words if w]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def build_faq(title: str, description: str, entries, price, store_url: str, repo, spec=None):
    """The questions a buyer actually types, answered from what the README says.

    Every answer here is assembled from facts the repository already states: the
    'Included formats' list, the price in the shared data file, and the two
    paragraphs every README carries about the plugin being self-contained and
    the samples not being in the repository. Nothing is invented, because an FAQ
    that drifts from the manual is worse than no FAQ at all.
    """
    faq = []

    plugin_formats = [e for e in entries if DECENT_SAMPLER_NAME not in e["name"].lower()]
    sampler = next((e for e in entries if DECENT_SAMPLER_NAME in e["name"].lower()), None)
    plugin_names = [e["name"] for e in plugin_formats]
    plugin_systems = []
    for entry in plugin_formats:
        for system in entry["systems"]:
            if system not in plugin_systems:
                plugin_systems.append(system)

    if description:
        faq.append((f"What is {title}?", strip_markdown(description)))

    if plugin_names:
        answer = (
            f"{title} is available as {join_words(plugin_names)}"
            + (f" for {join_words(plugin_systems)}" if plugin_systems else "")
        )
        if sampler:
            answer += (
                f", and as a Decent Sampler library"
                + (f" for {join_words(sampler['systems'])}" if sampler["systems"] else "")
            )
        faq.append((f"Which formats does {title} come in?", answer + "."))

    # Only claimed when the formats list actually says so, rather than assumed.
    other_systems = [s for s in (sampler["systems"] if sampler else []) if s not in plugin_systems]
    if plugin_systems == ["macOS"] and other_systems:
        faq.append((
            f"Does {title} run on Windows or Linux?",
            f"The plugin version is released for macOS only. On {join_words(other_systems)}, "
            f"the Decent Sampler version of {title} covers the same instrument.",
        ))

    if "VST3" in plugin_names and "AU" in plugin_names:
        faq.append((
            f"Does {title} work in Logic Pro and Ableton Live?",
            f"Yes. {title} is available as both VST3 and AU, so it loads in any macOS host "
            "that reads those formats, including Logic Pro, Ableton Live, Cubase, Reaper "
            "and Studio One.",
        ))

    if sampler:
        faq.append((
            f"Do I need Decent Sampler to use {title}?",
            "Not for the plugin version. It is a self-contained instrument with its samples "
            "embedded, so there are no external files to install or locate. The Decent Sampler "
            "version is a sample library, and that one needs the free Decent Sampler "
            "application.",
        ))

    spec = spec or {}
    if spec.get("sampleRate") and spec.get("bitDepth"):
        faq.append((
            f"What sample rate and bit depth is {title} recorded at?",
            f"The samples are {spec['sampleRate']}, {spec['bitDepth']}.",
        ))
    if spec.get("samples") and spec.get("size"):
        faq.append((
            f"How many samples does {title} contain?",
            f"{spec['samples']} sample files, {spec['size']} of audio. "
            "Impulse responses for the effects are not counted in that figure.",
        ))

    if price:
        free = float(price["minimum"]) == 0
        if free and price["payWhatYouWant"]:
            cost = f"{title} is free, and the store lets you pay what you want for it."
        elif free:
            cost = f"{title} is free."
        elif price["payWhatYouWant"]:
            cost = (
                f"{title} is pay what you want, from {format_price(price)}, "
                "so the price shown is the minimum rather than a fixed one."
            )
        else:
            cost = f"{title} costs {format_price(price)}."
        faq.append((f"How much does {title} cost?", cost))

    if store_url:
        faq.append((
            f"Where can I download {title}?",
            f"From the Dehli Musikk store at {store_url}.",
        ))

    if repo:
        samples = (
            "The audio files are not in the repository and come with the download."
            if price and float(price["minimum"]) == 0
            else "The audio files are not in the repository, because the samples are a paid product."
        )
        faq.append((
            f"Is the source code for {title} available?",
            f"Yes. The repository is public at {repo} and licensed under GPL-3.0. {samples}",
        ))

    return faq


def render_faq(faq) -> str:
    """Rendered so the visible answer and the Answer.text in the graph match word
    for word, which is what the structured data is required to claim."""
    if not faq:
        return ""
    items = "".join(
        f'<details class="faq-item"><summary>{html.escape(question)}</summary>'
        f'<div class="body"><p>{html.escape(answer)}</p></div></details>'
        for question, answer in faq
    )
    return (
        '<section id="faq">'
        '<h2><a class="anchor" href="#faq" aria-hidden="true">#</a>'
        "Frequently asked questions</h2>"
        f'<div class="faq">{items}</div></section>'
    )


def faq_node(faq, pages: str, title: str):
    if not faq:
        return None
    return {
        "@type": "FAQPage",
        "@id": pages + "#faq",
        "name": f"Frequently asked questions about {title}",
        "isPartOf": {"@id": pages},
        "mainEntity": [
            {
                "@type": "Question",
                "name": question,
                "acceptedAnswer": {"@type": "Answer", "text": answer},
            }
            for question, answer in faq
        ],
    }


# ── technical specification ──────────────────────────────────────────────────

SIZE_RE = re.compile(r"^([\d.]+)\s*(KB|MB|GB|TB)$", re.I)
SIZE_UNITS = {"kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12}
# The rows a README labels as impulse responses are part of the effects, not of
# the instrument's sampled sound, so they stay out of the sample totals.
IR_LABEL_RE = re.compile(r"impulse response", re.I)


def parse_size(text: str):
    """Bytes for "241.3 MB". Decimal units, the way the file sizes were read off."""
    match = SIZE_RE.match(strip_markdown(text))
    return float(match.group(1)) * SIZE_UNITS[match.group(2).lower()] if match else None


def format_size(size: float) -> str:
    if size >= 1e9:
        return f"{size / 1e9:.2f} GB"
    if size >= 1e6:
        return f"{size / 1e6:.1f} MB"
    return f"{size / 1e3:.0f} KB"


def spec_from_table(block):
    """Twelve READMEs state the specification as a table, a row per sample group."""
    header = [strip_markdown(cell).lower() for cell in block["header"]]

    def column(name):
        return next((n for n, cell in enumerate(header) if name in cell), None)

    rate_col, depth_col = column("sample rate"), column("bit depth")
    files_col, size_col = column("number of files"), column("file size")
    if rate_col is None or size_col is None:
        return None

    rates, depths, files, total = [], [], 0, 0.0
    for row in block["rows"]:
        if not row or IR_LABEL_RE.search(strip_markdown(row[0])):
            continue

        def cell(index):
            return strip_markdown(row[index]) if index is not None and index < len(row) else ""

        for value, seen in ((cell(rate_col), rates), (cell(depth_col), depths)):
            if value and value not in seen:
                seen.append(value)
        count = re.sub(r"\D", "", cell(files_col))
        if count:
            files += int(count)
        size = parse_size(cell(size_col))
        if size:
            total += size

    return build_spec(rates, depths, files, total)


def spec_from_list(block):
    """Omni-84 states the same facts as a bullet list rather than a table."""
    fields = {}
    for item in block["items"]:
        raw = strip_markdown(item["text"])
        if ":" in raw:
            key, value = raw.split(":", 1)
            fields[key.strip().lower()] = value.strip()
    if "sample rate" not in fields:
        return None
    count = re.sub(r"\D", "", fields.get("number of samples", ""))
    return build_spec(
        [fields["sample rate"]],
        [fields["bit depth"]] if fields.get("bit depth") else [],
        int(count) if count else 0,
        parse_size(fields.get("file size for samples", "")) or 0.0,
    )


def build_spec(rates, depths, files: int, total: float):
    spec = {}
    if rates:
        spec["sampleRate"] = join_words(rates)
    if depths:
        spec["bitDepth"] = join_words(depths)
    if files:
        spec["samples"] = files
    if total:
        spec["size"] = format_size(total)
    return spec or None


def parse_spec(section):
    """Sample rate, bit depth, sample count and total sample size.

    The README already renders this as a table a reader can see. Reading it here
    as well is what lets the same facts reach the page summary, the FAQ and the
    structured data, instead of being locked inside a pipe table halfway down.
    """
    if not section:
        return {}
    for block in section["blocks"]:
        found = None
        if block["type"] == "table" and block.get("header"):
            found = spec_from_table(block)
        elif block["type"] == "list":
            found = spec_from_list(block)
        if found:
            return found
    return {}


def build_glance(title: str, format_entries, systems, version, date, price, spec):
    """The facts worth reading before the manual, as (term, definition) pairs.

    The plugin formats and the Decent Sampler library get a row each whenever
    they run on different platforms, which they do: flattening them into one
    list of systems would say the plugin runs on Windows, and it does not.
    """
    rows = []
    plugin = [e for e in format_entries if DECENT_SAMPLER_NAME not in e["name"].lower()]
    sampler = next(
        (e for e in format_entries if DECENT_SAMPLER_NAME in e["name"].lower()), None
    )
    plugin_systems = []
    for entry in plugin:
        for system in entry["systems"]:
            if system not in plugin_systems:
                plugin_systems.append(system)

    if plugin and sampler and plugin_systems != sampler["systems"]:
        rows.append((
            "Plugin",
            join_words([e["name"] for e in plugin])
            + (f" for {join_words(plugin_systems)}" if plugin_systems else ""),
        ))
        rows.append(("Decent Sampler", join_words(sampler["systems"]) or "Yes"))
    else:
        names = [e["name"] for e in format_entries]
        if names:
            rows.append(("Formats", join_words(names)))
        if systems:
            rows.append(("Systems", join_words(systems)))
    if version:
        rows.append(("Version", f"{version} ({date})" if date else version))
    if price:
        rows.append(("Price", price_note(price).rstrip(".")))
    if spec.get("sampleRate"):
        rows.append(("Sample rate", spec["sampleRate"]))
    if spec.get("bitDepth"):
        rows.append(("Bit depth", spec["bitDepth"]))
    if spec.get("samples"):
        rows.append(("Samples", f"{spec['samples']:,} files".replace(",", " ")))
    if spec.get("size"):
        rows.append(("Sample content", spec["size"]))
    return rows


def render_glance(rows) -> str:
    if not rows:
        return ""
    pairs = "".join(
        f"<dt>{html.escape(term)}</dt><dd>{html.escape(definition)}</dd>"
        for term, definition in rows
    )
    return (
        '<section id="at-a-glance">'
        '<h2><a class="anchor" href="#at-a-glance" aria-hidden="true">#</a>At a glance</h2>'
        f'<dl class="specs">{pairs}</dl></section>'
    )


def split_releases(section, renderer: Renderer):
    """Render the release-notes section as one <details> per version."""
    if not section:
        return ""
    parts = []
    lead, groups, current = [], [], None
    for block in section["blocks"]:
        if block["type"] == "heading" and block["level"] == 3:
            current = {"title": block["text"], "blocks": []}
            groups.append(current)
        elif current:
            current["blocks"].append(block)
        else:
            lead.append(block)

    if lead:
        parts.append(renderer.blocks(lead))
    for n, group in enumerate(groups):
        match = VERSION_RE.match(strip_markdown(group["title"]))
        label = f"Version {match.group(1)}" if match else strip_markdown(group["title"])
        date = f'<span class="date">{html.escape(match.group(2))}</span>' if match and match.group(2) else ""
        parts.append(
            f'<details class="release"{" open" if n == 0 else ""}>'
            f"<summary>{html.escape(label)}{date}</summary>"
            f'<div class="body">{renderer.blocks(group["blocks"])}</div>'
            f"</details>"
        )
    return "\n".join(parts)


def section_subheadings(section):
    return [
        {"text": strip_markdown(b["text"]), "slug": b.get("slug") or slugify(b["text"])}
        for b in section["blocks"]
        if b["type"] == "heading" and b["level"] == 3
    ]


# ── plugin metadata ──────────────────────────────────────────────────────────

def match_key(text: str) -> str:
    """Normalize a product name so "MaskinTrommer", the Pages repository name
    and the directory "maskintrommer-plugin" all resolve to the same entry."""
    return re.sub(r"[^a-z0-9]+", "", text.lower().replace("-plugin", ""))


def load_plugin_data(path: Path):
    if not path.is_file():
        warn(f"{path.name} not found — store links, sameAs and videos will be omitted")
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{path}: {exc}")
    return {match_key(entry["title"]): entry for entry in entries if entry.get("title")}


def lookup_extra(data, names):
    for name in names:
        entry = data.get(match_key(name))
        if entry:
            return entry
    return {}


CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}

# Octicons, inlined so the page stays self contained (16x16 viewBox).
ICON_STAR = (
    "M8 .25a.75.75 0 0 1 .673.418l1.882 3.815 4.21.612a.75.75 0 0 1 .416 1.279l-3.046 2.97."
    "719 4.192a.75.75 0 0 1-1.088.791L8 12.347l-3.766 1.98a.75.75 0 0 1-1.088-.79l.72-4.194L."
    "818 6.374a.75.75 0 0 1 .416-1.28l4.21-.611L7.327.668A.75.75 0 0 1 8 .25Z"
)
ICON_GITHUB = (
    "M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13."
    "01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2."
    "36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03"
    "-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.6"
    "4 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27."
    "38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55."
    "38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"
)


def normalize_price(value, currency_fallback: str = "USD"):
    """Accept either {"minimum": ..., "currency": ..., "payWhatYouWant": ...} from
    the shared data file or a bare price from a plugin's site.json."""
    if value is None or value == "":
        return None
    if not isinstance(value, dict):
        return {"minimum": str(value), "currency": currency_fallback, "payWhatYouWant": False}
    minimum = value.get("minimum", value.get("price"))
    if minimum is None:
        return None
    return {
        "minimum": str(minimum),
        "currency": value.get("currency", currency_fallback),
        "payWhatYouWant": bool(value.get("payWhatYouWant")),
    }


def format_price(price) -> str:
    amount = price["minimum"]
    if float(amount) == 0:
        return "Free"
    symbol = CURRENCY_SYMBOLS.get(price["currency"])
    return f"{symbol}{amount}" if symbol else f"{amount} {price['currency']}"


def price_badge(price) -> str:
    """The store sells pay what you want, so a flat price would be a lie."""
    if float(price["minimum"]) == 0:
        return "Free" if not price["payWhatYouWant"] else "Free · pay what you want"
    formatted = format_price(price)
    return f"From {formatted}" if price["payWhatYouWant"] else formatted


def price_note(price) -> str:
    if float(price["minimum"]) == 0:
        return "Free, or pay what you want." if price["payWhatYouWant"] else "Free."
    if price["payWhatYouWant"]:
        return f"Pay what you want, from {format_price(price)}."
    return format_price(price)


def last_sunday(year: int, month: int) -> datetime.date:
    weeks = calendar.monthcalendar(year, month)
    return datetime.date(year, month, max(week[calendar.SUNDAY] for week in weeks))


def upload_datetime(value: str):
    """Google wants uploadDate as a full ISO 8601 datetime with a timezone; the
    data file only carries a calendar date. Noon keeps that date intact in every
    timezone a viewer might be in, and the offset is Norway's for the date in
    question (EU summer time runs from the last Sunday in March to the last in
    October), so the value stays deterministic without a tzdata dependency."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        return None
    try:
        date = datetime.date(*(int(part) for part in value.split("-")))
    except ValueError:
        warn(f"ignoring an impossible uploadDate: {value}")
        return None
    summer = last_sunday(date.year, 3) <= date < last_sunday(date.year, 10)
    return f"{value}T12:00:00{'+02:00' if summer else '+01:00'}"


# Google reads the duration as ISO 8601 in the structured data and as whole
# seconds in the video sitemap, so it is stored once, in the ISO form, and
# converted for the sitemap rather than written out twice.
DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
MAX_SITEMAP_DURATION = 28800  # what the video sitemap schema allows, eight hours


def duration_seconds(value):
    """Seconds for an ISO 8601 duration such as PT2M14S, or None if unusable."""
    match = DURATION_RE.fullmatch((value or "").strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def format_duration(seconds: int) -> str:
    """2:14, or 1:02:03 once there is an hour of it."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


_DURATION_WARNED = set()


def video_duration(video, label: str):
    """(ISO 8601, seconds) for a video, warning once if it is missing or wrong.

    The watch page and the sitemap both ask, so the complaint is kept to one per
    video: thirteen useful lines rather than twenty-six that repeat themselves.
    """
    def grumble(message: str) -> None:
        if label not in _DURATION_WARNED:
            _DURATION_WARNED.add(label)
            warn(message)

    raw = (video.get("duration") or "").strip()
    if not raw:
        grumble(f"{label}: no duration, and Google asks for one on a video page")
        return None, None
    seconds = duration_seconds(raw)
    if not seconds:
        grumble(f"{label}: duration {raw!r} is not an ISO 8601 length such as PT2M14S")
        return None, None
    if seconds > MAX_SITEMAP_DURATION:
        grumble(f"{label}: duration {raw} is longer than a video sitemap allows")
        return raw, None
    return raw, seconds


_CLIP_WARNED = set()


def video_clips(video, total_seconds, label: str):
    """The video's chapters as [(name, start, end)], in order and validated.

    A clip that runs past the end of the video is rejected outright by Google,
    so an end offset beyond the duration is pulled back to it and reported: the
    chapter is still true, only its stated end was wrong.
    """
    clips = []
    for clip in video.get("clips") or []:
        name = pick_language(clip.get("name"))
        start = clip.get("startOffset")
        if not name or not isinstance(start, int) or start < 0:
            continue
        end = clip.get("endOffset")
        if isinstance(end, int) and total_seconds and end > total_seconds:
            # Asked for by the watch page and by the markdown twin, so said once.
            complaint = f"{label}: chapter {name!r} ends at {end}s but the video is {total_seconds}s long"
            if complaint not in _CLIP_WARNED:
                _CLIP_WARNED.add(complaint)
                warn(complaint)
            end = total_seconds
        if isinstance(end, int) and end <= start:
            continue
        clips.append((name, start, end))
    clips.sort(key=lambda clip: clip[1])
    # One chapter is not a chapter list, it is the video.
    return clips if len(clips) > 1 else []


def clip_nodes(clips, url: str):
    """schema.org Clip parts, each addressing the start time on this page."""
    parts = []
    for name, start, end in clips:
        node = {
            "@type": "Clip",
            "name": name,
            "startOffset": start,
            "url": f"{url}?t={start}",
        }
        if end:
            node["endOffset"] = end
        parts.append(node)
    return parts


def youtube_id(url: str):
    match = YOUTUBE_ID_RE.search(url or "")
    return match.group(1) if match else None


def pick_language(value, language: str = "en"):
    """The data file carries {"en": ..., "no": ...}; these pages are English."""
    if isinstance(value, dict):
        return value.get(language) or next(iter(value.values()), "")
    return value or ""


ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_COMMIT_DATE_CACHE = {}


def last_commit_date(path: Path):
    """YYYY-MM-DD of the commit that last touched path, or None.

    Unlike the remote lookup this is allowed to fail quietly: a page with a
    slightly stale lastmod is a far smaller problem than one that stops being
    written, and the release date is a reasonable thing to fall back to.
    """
    if path in _COMMIT_DATE_CACHE:
        return _COMMIT_DATE_CACHE[path]
    date = None
    if path.is_file():
        try:
            done = subprocess.run(
                ["git", "-C", str(path.parent), "log", "-1",
                 "--date=short", "--format=%cd", "--", path.name],
                capture_output=True, text=True,
            )
            found = done.stdout.strip()
            if done.returncode == 0 and ISO_DATE_RE.fullmatch(found):
                date = found
        except OSError:
            pass
    _COMMIT_DATE_CACHE[path] = date
    return date


def page_modified(plugin_dir: Path, release_date):
    """When this page last changed, which is when the things it is built from
    last changed: the README it renders, the shared product data, and the
    generator and stylesheet that decide what the page looks like.

    The release date is not that. A README gets corrected and expanded between
    releases, and every page was claiming the release date as its dateModified
    while carrying text written weeks later.
    """
    dates = [
        date
        for date in (
            last_commit_date(plugin_dir / "README.md"),
            last_commit_date(PLUGIN_DATA),
            last_commit_date(Path(__file__).resolve()),
            last_commit_date(SITE_DIR / "style.css"),
        )
        if date
    ]
    if dates:
        return max(dates)
    return release_date if ISO_DATE_RE.fullmatch(release_date or "") else None


def git_remote(plugin_dir: Path):
    """(origin URL, why it could not be read).

    The two are told apart on purpose. A repository with no origin is a fact
    about that repository; git failing to run is a fact about this machine, and
    is usually temporary. Reporting both as "no git remote" made a transient
    failure look like a settled answer and quietly stripped the page.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(plugin_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True,
        )
    except OSError as exc:
        return None, f"git could not be run: {exc}"
    if done.returncode != 0:
        detail = done.stderr.strip().splitlines()
        return None, detail[-1] if detail else f"git exited with {done.returncode}"
    url = done.stdout.strip()
    return (url, None) if url else (None, "origin is configured with no URL")


_STYLESHEET = None


def stylesheet() -> str:
    """The shared stylesheet, for inlining into the page.

    At 3.6 kB compressed it is cheaper to send with the HTML than to make the
    browser discover it, request it and wait for it before painting anything.
    The hero image is preloaded precisely so first paint is not held up, and an
    external stylesheet put that wait straight back in front of it.
    """
    global _STYLESHEET
    if _STYLESHEET is None:
        _STYLESHEET = (SITE_DIR / "style.css").read_text(encoding="utf-8").strip()
    return _STYLESHEET


_META_CACHE = {}


def read_meta(plugin_dir: Path):
    cached = _META_CACHE.get(plugin_dir)
    if cached is not None:
        return dict(cached)
    meta = _read_meta_uncached(plugin_dir)
    _META_CACHE[plugin_dir] = dict(meta)
    return meta


def _read_meta_uncached(plugin_dir: Path):
    meta = {"product": plugin_dir.name.replace("-plugin", ""), "version": None, "repo": None}

    cmake = plugin_dir / "CMakeLists.txt"
    if cmake.is_file():
        text = cmake.read_text(encoding="utf-8")
        product = re.search(r'PRODUCT_NAME\s+"([^"]+)"', text)
        version = re.search(r"VERSION\s+([0-9][0-9.]*)", text)
        if product:
            meta["product"] = product.group(1)
        if version:
            meta["version"] = version.group(1)

    url, problem = git_remote(plugin_dir)
    if url:
        match = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?$", url)
        if match:
            owner, repo = match.group(1), match.group(2)
            meta["owner"] = owner
            meta["slug"] = f"{owner}/{repo}"
            meta["repo"] = f"https://github.com/{owner}/{repo}"
            meta["pages"] = f"https://{owner}.github.io/{repo}/"
        else:
            problem = f"origin is not a GitHub remote ({url})"
    if problem:
        # Everything that makes the page findable hangs off this URL: the
        # canonical link, the structured data, the sitemap and the watch pages.
        # A checkout that fails to answer is a broken run, not a plugin without
        # a remote, and publishing the stripped page would be the worse outcome.
        if (plugin_dir / ".git").exists():
            die(
                f"{plugin_dir.name}: could not read the GitHub remote.\n"
                f"  {problem}\n"
                "  The page would go out with no canonical URL, no structured data\n"
                "  and no sitemap, so nothing was written. Run it again."
            )
        warn(f"{plugin_dir.name}: {problem}, so GitHub links will be omitted")

    overrides = plugin_dir / "site.json"
    if overrides.is_file():
        try:
            meta.update(json.loads(overrides.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            die(f"{overrides}: {exc}")
    return meta


def pick_hero(plugin_dir: Path, meta):
    """The largest screenshot — in practice the full-GUI overview shot."""
    override = meta.get("heroImage")
    if override:
        path = plugin_dir / override.lstrip("/")
        return f"/{override.lstrip('/')}" if path.is_file() else None

    shots = plugin_dir / "Screenshots"
    if not shots.is_dir():
        return None
    best, best_area = None, 0
    for path in sorted(shots.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        size = image_size(path)
        area = size[0] * size[1] if size else 0
        if area > best_area:
            best, best_area = path, area
    return f"/Screenshots/{best.name}" if best else None


def hero_alt(meta, readme_text: str, hero_src, title: str) -> str:
    """Alt text for the lead screenshot, in the README's own words.

    The same sentence used to be asserted about all thirteen heroes: that the
    shot showed the controls and the on-screen keyboard. The hero is whichever
    screenshot is largest, or whatever heroImage names, and for EDB-Orgel that
    is a tab with neither. The README already names every screenshot it embeds,
    so the honest description is the one written next to that very image.
    """
    if meta.get("heroAlt"):
        return meta["heroAlt"]
    if hero_src:
        for alt, src in IMG_RE.findall(readme_text):
            if src.strip() == hero_src.strip() and alt.strip():
                alt = alt.strip()
                return alt if title.lower() in alt.lower() else f"{title} {alt}"
    return f"The {title} plugin interface"


def read_title(plugin_dir: Path) -> str:
    for line in (plugin_dir / "README.md").read_text(encoding="utf-8").split("\n"):
        heading = HEADING_RE.match(line)
        if heading and len(heading.group(1)) == 1:
            return heading.group(2)
    return plugin_dir.name.replace("-plugin", "")


def sibling_plugins(current: Path, data):
    """The other instruments in the workspace, for the cross-link strip.

    Internal links between the product pages are worth real SEO, and they only
    need each sibling's own git remote to work out its Pages URL.
    """
    siblings = []
    for plugin_dir in sorted(ROOT.glob("*-plugin")):
        if plugin_dir.resolve() == current.resolve() or not (plugin_dir / "README.md").is_file():
            continue
        meta = read_meta(plugin_dir)
        if not meta.get("pages"):
            continue
        title = read_title(plugin_dir)
        meta.update(lookup_extra(data, [title, meta["product"], plugin_dir.name]))
        hero = pick_hero(plugin_dir, meta)
        siblings.append({
            "title": title,
            "pages": meta["pages"],
            "name": plugin_dir.name,
            "hero": plugin_dir / hero.lstrip("/") if hero else None,
        })
    return siblings


def render_more(siblings, images: Images) -> str:
    if not siblings:
        return ""
    cards = []
    for sibling in siblings:
        thumb = ""
        if sibling["hero"] and sibling["hero"].is_file():
            entry = images.register_file(sibling["hero"], f"more-{sibling['name']}", THUMB_WIDTH)
            size = entry["size"] or (0, 0)
            dims = f' width="{size[0]}" height="{size[1]}"' if size[0] else ""
            thumb = f'<img src="{entry["url"]}" alt="" loading="lazy"{dims}>'
        cards.append(
            f'<li><a href="{sibling["pages"]}">{thumb}'
            f'<span class="label">{html.escape(sibling["title"])}</span></a></li>'
        )
    return (
        '<aside class="more" aria-labelledby="more-title">'
        '<h2 id="more-title">More instruments from Dehli Musikk</h2>'
        f'<ul class="more-grid">{"".join(cards)}</ul></aside>'
    )


def normalize_videos(value):
    """One video or several: the data file accepts an object or a list."""
    if not value:
        return []
    videos = value if isinstance(value, list) else [value]
    return [v for v in videos if v and youtube_id(v.get("contentUrl", ""))]


def relative_watch_path(index: int) -> str:
    return "video/" if index == 0 else f"video/{index + 1}/"


def render_video(videos, title: str, pages=None) -> str:
    """A click-to-load facade: no YouTube request until the reader asks for it.

    No VideoObject is published here — the video is not this page's main content,
    so it belongs to the watch page under video/, which this links to.
    """
    videos = normalize_videos(videos)
    if not videos:
        return ""

    players = []
    for index, video in enumerate(videos):
        ident = youtube_id(video["contentUrl"])
        name = pick_language(video.get("name")) or f"{title} demo"
        description = pick_language(video.get("description"))
        # Warned about once per run, by the structured data, not here as well.
        seconds = duration_seconds(video.get("duration"))
        label = html.escape(f"Play video: {name}", quote=True)
        players.append(
            '<figure class="video-item">'
            + f'<div class="video" data-youtube="{ident}">'
            f'<button class="video-play" type="button" aria-label="{label}">'
            f'<img src="https://i.ytimg.com/vi/{ident}/hqdefault.jpg" alt="" loading="lazy" '
            'width="480" height="360">'
            '<span class="play" aria-hidden="true"></span></button></div>'
            f'<figcaption class="video-caption"><strong>{html.escape(name)}</strong>'
            + (f' <span class="duration">{format_duration(seconds)}</span>' if seconds else "")
            + (f". {html.escape(description)}" if description and len(videos) > 1 else "")
            + (
                f' <a href="{html.escape(relative_watch_path(index), quote=True)}">Video page</a> ·'
                if pages
                else ""
            )
            + f' <a href="{html.escape(video["contentUrl"], quote=True)}" target="_blank" '
            'rel="noopener">Watch on YouTube</a></figcaption></figure>'
        )

    lead = ""
    if len(videos) == 1:
        description = pick_language(videos[0].get("description"))
        lead = f"<p>{html.escape(description)}</p>" if description else ""

    return (
        '<section id="demo-video">'
        '<h2><a class="anchor" href="#demo-video" aria-hidden="true">#</a>'
        + ("Videos" if len(videos) > 1 else "Video")
        + "</h2>"
        + lead
        + ('<div class="video-grid">' + "".join(players) + "</div>" if len(videos) > 1 else players[0])
        + "</section>"
    )


# ── markdown twin ────────────────────────────────────────────────────────────

def absolutize_images(text: str, images: Images, pages: str) -> str:
    """Point the README's image paths at the copies published beside the page.

    The README writes /Screenshots/Chords.png, which resolves against the
    repository rather than the site. Every one of those has already been
    re-encoded into img/ by the time this runs, so the mapping is a lookup.
    """
    def replace(match):
        entry = images.by_src.get(match.group(2).strip())
        if not entry:
            return match.group(0)
        return f"![{match.group(1)}]({pages + entry['url']})"

    return IMG_RE.sub(replace, text)


def markdown_twin(ctx, readme_text: str) -> str:
    """The page as markdown, for readers that would rather not parse HTML.

    The README is already the manual, so it is carried over whole rather than
    re-rendered: what is added in front of it is the part a reader of the page
    gets and a reader of the raw README does not, which is the summary, the
    links off the page and the FAQ.
    """
    pages = ctx["pages"]
    out = [f"# {ctx['title']}", ""]
    if ctx["description"]:
        out += [f"> {strip_markdown(ctx['description'])}", ""]
    out += [
        f"A sample instrument by {BRAND}. This is the markdown version of {pages}, "
        "generated from the repository README.",
        "",
    ]

    if ctx["glance"]:
        out += ["## Key facts", ""]
        out += [f"- {term}: {definition}" for term, definition in ctx["glance"]]
        out.append("")

    links = [("Product page", pages)]
    if ctx.get("product_page"):
        links.append((f"{ctx['title']} at {BRAND}", ctx["product_page"]))
    links.append(("Get it", ctx["store_url"]))
    if ctx.get("repo"):
        links.append(("Source on GitHub", ctx["repo"]))
    chapters = []
    for index, video in enumerate(ctx.get("videos") or []):
        name = pick_language(video.get("name")) or f"{ctx['title']} demo"
        url = watch_url(pages, index)
        links.append((name, url))
        _, seconds = video_duration(video, f"{ctx['title']} video")
        for clip_name, start, _end in video_clips(video, seconds, f"{ctx['title']} video"):
            chapters.append(f"- [{format_duration(start)} {clip_name}]({url}?t={start})")
    out += ["## Links", ""]
    out += [f"- [{name}]({url})" for name, url in links]
    out.append("")

    if chapters:
        out += ["## Video chapters", ""] + chapters + [""]

    if ctx["faq"]:
        out += ["## Frequently asked questions", ""]
        for question, answer in ctx["faq"]:
            out += [f"### {question}", "", answer, ""]

    # The README's own headings follow as siblings of the ones above, so its H1
    # goes: the document already has one. The line linking to the product page
    # goes for the same reason the rendered page drops it, which is that the
    # reader is holding that page already.
    body = readme_text.replace("\r\n", "\n")
    body = re.sub(r"\A\s*#\s+[^\n]*\n", "", body)
    body = "\n".join(
        line for line in body.split("\n") if not is_self_link(line, pages)
    ).strip()
    out += ["---", "", absolutize_images(body, ctx["images"], pages), ""]
    return "\n".join(out)


SELF_LINK_RE = re.compile(r"^\s*\**\[[^\]]+\]\(([^)\s]+)\)\**\s*$")


def is_self_link(line: str, pages: str) -> bool:
    """True for a line that is nothing but a link back to this very page."""
    match = SELF_LINK_RE.match(line)
    return bool(match and match.group(1).rstrip("/") == pages.rstrip("/"))


def build_root_llms(data) -> None:
    """An llms.txt index of the instruments, for the user site to publish.

    Written next to the sitemap index and for the same reason: one URL at the
    domain root that leads to all thirteen, rather than thirteen that have to be
    found first.
    """
    rows = []
    for plugin_dir in sorted(ROOT.glob("*-plugin")):
        if not (plugin_dir / "README.md").is_file():
            continue
        meta = read_meta(plugin_dir)
        if not meta.get("pages"):
            continue
        title = read_title(plugin_dir)
        extra = lookup_extra(data, [title, meta["product"], plugin_dir.name])
        description = strip_markdown(extra.get("description") or "")
        # The shared descriptions open with the product's own name, which reads
        # as a stutter once the name is already the link text.
        description = re.sub(rf"^{re.escape(title)}:\s*", "", description, flags=re.I)
        if description:
            description = description[0].upper() + description[1:]
        rows.append((title, meta["pages"], description))
    if not rows:
        return

    out = [
        f"# {BRAND} sample instruments",
        "",
        f"> Sampled instruments by {AUTHOR_NAME}, each released both as a macOS plugin "
        "(VST3, AU and standalone) and as a Decent Sampler library for macOS, Windows "
        "and Linux.",
        "",
        f"Every instrument has its own product page with the full manual. The store is "
        f"{DEFAULT_STORE_URL} and the label's site is {BRAND_URL}",
        "",
        "## Instruments",
        "",
    ]
    out += [
        f"- [{title}]({pages}){f': {description}' if description else ''}"
        for title, pages, description in rows
    ]
    out += ["", "## Full documentation in markdown", ""]
    out += [f"- [{title}]({pages}llms.txt)" for title, pages, _ in rows]
    out.append("")

    out_dir = SITE_DIR / "user-site"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "llms.txt").write_text("\n".join(out), encoding="utf-8")
    info(f"llms.txt index for {len(rows)} instruments → {out_dir.relative_to(ROOT)}/")


# ── page assembly ────────────────────────────────────────────────────────────

def watch_url(pages: str, index: int) -> str:
    """Watch pages live at video/ and video/2/, video/3/ … under the product."""
    return f"{pages}video/" if index == 0 else f"{pages}video/{index + 1}/"


def watch_dir(out_dir: Path, index: int) -> Path:
    return out_dir / "video" if index == 0 else out_dir / "video" / str(index + 1)


def build_watch_page(video, index: int, ctx) -> None:
    """A page whose main content is the video, which is what Google means by a
    watch page: the player is real HTML the crawler can see, not a facade, and
    the surrounding text is about the video rather than the instrument manual."""
    ident = youtube_id(video["contentUrl"])
    name = pick_language(video.get("name")) or f"{ctx['title']} demo"
    description = pick_language(video.get("description")) or f"A demonstration of {ctx['title']}."
    url = watch_url(ctx["pages"], index)
    up = "../" * (2 if index else 1)
    esc = lambda value: html.escape(str(value), quote=True)  # noqa: E731

    badges = []
    watch_seconds = duration_seconds(video.get("duration"))
    if watch_seconds:
        badges.append(f'<span class="badge">{esc(format_duration(watch_seconds))}</span>')
    if ctx["price"]:
        badges.append(f'<span class="badge badge-price">{esc(price_badge(ctx["price"]))}</span>')
    if ctx["version"]:
        badges.append(f'<span class="badge">Version {esc(ctx["version"])}</span>')
    for fmt in ctx["formats"]:
        badges.append(f'<span class="badge">{esc(fmt)}</span>')

    # Built by name, not by position in the graph: uploadDate was once assigned
    # through graph[1] and silently moved onto the breadcrumb when a node was
    # inserted ahead of it.
    video_node = {
        "@type": "VideoObject",
        "@id": url + "#video",
        "name": name,
        "description": description,
        "url": url,
        "thumbnailUrl": [f"https://i.ytimg.com/vi/{ident}/hqdefault.jpg"],
        "contentUrl": video["contentUrl"],
        "embedUrl": f"https://www.youtube.com/embed/{ident}",
        "mainEntityOfPage": {"@id": url},
        "about": {"@id": ctx["ids"]["product"]},
        "author": {"@id": AUTHOR_ID},
        "publisher": {"@id": PUBLISHER_ID},
    }
    uploaded = upload_datetime(video.get("uploadDate"))
    if uploaded:
        video_node["uploadDate"] = uploaded
    else:
        warn(f"{ctx['title']}: video has no usable uploadDate, and Google requires it")
    iso, seconds = video_duration(video, f"{ctx['title']} video")
    if iso:
        video_node["duration"] = iso
    clips = video_clips(video, seconds, f"{ctx['title']} video")
    if clips:
        video_node["hasPart"] = clip_nodes(clips, url)

    graph = [
        {
            "@type": "WebPage",
            "@id": url,
            "url": url,
            "name": name,
            "description": description,
            "inLanguage": "en",
            "isPartOf": {"@id": ctx["ids"]["website"]},
            "breadcrumb": {"@id": url + "#breadcrumb"},
            "mainEntity": {"@id": url + "#video"},
            **({"dateModified": ctx["modified"]} if ctx.get("modified") else {}),
        },
        breadcrumb_node(url + "#breadcrumb", [
            (AUTHOR_NAME, site_root(ctx["pages"])),
            (ctx["title"], ctx["pages"]),
            ("Video" if index == 0 else f"Video {index + 1}", url),
        ]),
        video_node,
    ] + author_nodes()

    icon_tag = f'<img src="{up}img/icon.png" alt="">' if ctx["icon"] else ""
    favicon = f'<link rel="icon" href="{up}img/icon.png">' if ctx["icon"] else ""

    # Real links, because that is what the Clip markup points at and what has to
    # work with scripting off. The click handler below only saves a reload.
    chapter_list = ""
    if clips:
        rows = "".join(
            f'<li><a href="?t={start}" data-start="{start}">'
            f'<span class="at">{format_duration(start)}</span>'
            f'<span class="what">{esc(clip_name)}</span></a></li>'
            for clip_name, start, _end in clips
        )
        chapter_list = (
            '  <section class="chapters" aria-labelledby="chapters-title">'
            '<h2 id="chapters-title">Chapters</h2>'
            f"<ol>{rows}</ol></section>"
        )

    # The Clip markup promises that ?t=<seconds> starts the video there, so the
    # page has to keep that promise. The iframe is left plain in the markup for
    # the crawler and only rewritten once a start time is actually asked for.
    chapter_script = ""
    if clips:
        chapter_script = """<script>
(function () {
  var frame = document.querySelector('.watch iframe');
  if (!frame) return;
  var base = frame.src;

  function play(seconds, autoplay) {
    frame.src = base + '?start=' + seconds + (autoplay ? '&autoplay=1' : '');
  }

  var asked = new URLSearchParams(location.search).get('t');
  if (asked !== null && /^\\d+$/.test(asked)) play(asked, false);

  document.querySelectorAll('.chapters a[data-start]').forEach(function (link) {
    link.addEventListener('click', function (event) {
      event.preventDefault();
      play(link.dataset.start, true);
      history.replaceState(null, '', link.getAttribute('href'));
      frame.scrollIntoView({ block: 'nearest' });
    });
  });
}());
</script>"""

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(name)} | {BRAND}</title>
<meta name="description" content="{esc(summarize(description))}">
<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">
<meta name="theme-color" content="#a35a2a" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#17161a" media="(prefers-color-scheme: dark)">
<link rel="canonical" href="{esc(url)}">
<meta property="og:type" content="video.other">
<meta property="og:site_name" content="{BRAND}">
<meta property="og:title" content="{esc(name)}">
<meta property="og:description" content="{esc(summarize(description))}">
<meta property="og:url" content="{esc(url)}">
<meta property="og:image" content="https://i.ytimg.com/vi/{ident}/hqdefault.jpg">
<meta property="og:video" content="https://www.youtube.com/watch?v={ident}">
<meta property="og:video:url" content="https://www.youtube.com/embed/{ident}">
<meta property="og:video:type" content="text/html">
<meta name="twitter:card" content="player">
<meta name="twitter:title" content="{esc(name)}">
<meta name="twitter:description" content="{esc(summarize(description))}">
<meta name="twitter:image" content="https://i.ytimg.com/vi/{ident}/hqdefault.jpg">
<meta name="twitter:image:alt" content="{esc(name)}">
{favicon}
<style>
{stylesheet()}
</style>
<script type="application/ld+json">
{json.dumps({"@context": "https://schema.org", "@graph": graph}, indent=2, ensure_ascii=False)}
</script>
</head>
<body>

<header class="topbar">
  {icon_tag}
  <a class="name" href="{up}">{esc(ctx["title"])}</a>
  <span class="spacer"></span>
  <a class="btn btn-primary btn-sm" href="{esc(ctx["store_url"])}" target="_blank" rel="noopener">Get it</a>
</header>

<main class="watch">
  <h1>{esc(name)}</h1>
  <div class="video">
    <iframe src="https://www.youtube.com/embed/{ident}" title="{esc(name)}"
      allow="accelerometer; encrypted-media; picture-in-picture" allowfullscreen></iframe>
  </div>
  <p class="lead">{esc(description)}</p>
  <div class="badges">{"".join(badges)}</div>
{chapter_list}
  <p>{esc(ctx["tagline"])}</p>
  <div class="cta">
    <a class="btn btn-primary" href="{esc(ctx["store_url"])}" target="_blank" rel="noopener">{esc(ctx["cta_label"])}</a>
    <a class="btn" href="{up}">{esc(ctx["title"])} documentation</a>
    <a class="btn" href="https://www.youtube.com/watch?v={ident}" target="_blank" rel="noopener">Watch on YouTube</a>
  </div>
</main>

<footer>
  <nav>
    <a href="{up}">Product page</a>
    <a href="{esc(ctx["store_url"])}" target="_blank" rel="noopener">Store</a>
    <a href="{BRAND_URL}" target="_blank" rel="noopener">{BRAND}</a>
  </nav>
  <p>{esc(name)} is a demonstration of {esc(ctx["title"])}, a sample instrument by {BRAND}.</p>
</footer>
{chapter_script}

</body>
</html>
"""
    directory = watch_dir(ctx["out_dir"], index)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(page, encoding="utf-8")


def build_root_sitemap(data) -> None:
    """A sitemap index for the user site, benjamindehli.github.io.

    A robots.txt only counts at the domain root, so the one each project site
    generates is never read and none of the plugin sitemaps are discoverable on
    their own. Publishing this index from the user site's own repository is what
    makes all thirteen reachable from one URL. Written for every plugin in the
    workspace, not only the ones being built, so a single plugin run cannot
    truncate the index.

    No robots.txt is written here: the user site already has one, with the
    sitemaps of its other projects in it, and a generated file that has to be
    copied over the top of it would quietly drop them.
    """
    sites, origin = [], None
    for plugin_dir in sorted(ROOT.glob("*-plugin")):
        if not (plugin_dir / "README.md").is_file():
            continue
        meta = read_meta(plugin_dir)
        if not meta.get("pages"):
            continue
        sites.append(meta["pages"])
        origin = f"https://{meta['owner']}.github.io/"
    if not sites:
        return

    out_dir = SITE_DIR / "user-site"
    out_dir.mkdir(parents=True, exist_ok=True)
    index = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <sitemap>\n    <loc>{pages}sitemap.xml</loc>\n  </sitemap>\n" for pages in sites)
        + "</sitemapindex>\n"
    )
    (out_dir / "sitemap-index.xml").write_text(index, encoding="utf-8")
    host = origin.split("//")[1].rstrip("/")
    stale = out_dir / "robots.txt"
    if stale.is_file():
        stale.unlink()  # written by an older version, and it overwrote a richer one
    (out_dir / "README.md").write_text(
        "# Files for the user site\n\n"
        f"Generated by `./dmse site`. The user site is the `{host}` repository, an\n"
        "Astro project that publishes from `docs/`, so copy `sitemap-index.xml` and\n"
        "`llms.txt` into its `public/` folder and let the build carry them to the domain\n"
        "root.\n\n"
        "`llms.txt` is the index an answer engine reads to find the instruments: one entry\n"
        "per product with a one-line description, and a link to the full markdown manual\n"
        "each product site publishes at `/<Repo>/llms.txt`. If the user site grows a wider\n"
        "`llms.txt` of its own, keep the instrument sections and add the rest around them\n"
        "rather than keeping two competing files.\n\n"
        "A `robots.txt` is only honoured at the root of a domain, so the one each plugin site\n"
        f"generates at `/<Repo>/robots.txt` is never read by a crawler. The `{host}` one is,\n"
        "and it needs a single line added so the index is found:\n\n"
        f"    Sitemap: {origin}sitemap-index.xml\n\n"
        "That one line makes the sitemap of every plugin site discoverable, including the watch\n"
        "pages and their video entries, rather than needing thirteen manual submissions in Search\n"
        "Console. Add it alongside the sitemaps already listed there rather than replacing the\n"
        "file. Re-run `./dmse site` and copy the index again whenever a plugin is added or\n"
        "removed.\n",
        encoding="utf-8",
    )
    info(f"Root sitemap index for {len(sites)} sites → {out_dir.relative_to(ROOT)}/")


def build_404_page(ctx) -> None:
    """GitHub Pages serves this for any missing path under the site, including
    deep ones, and the browser keeps that URL — so every asset link here has to
    be root relative rather than relative to the page."""
    base = urllib.parse.urlparse(ctx["pages"]).path  # e.g. /Omni-84/
    esc = lambda value: html.escape(str(value), quote=True)  # noqa: E731
    icon_tag = f'<img src="{base}img/icon.png" alt="">' if ctx["icon"] else ""
    favicon = f'<link rel="icon" href="{base}img/icon.png">' if ctx["icon"] else ""

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Page not found | {esc(ctx["title"])}</title>
<meta name="robots" content="noindex, follow">
<meta name="theme-color" content="#a35a2a" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#17161a" media="(prefers-color-scheme: dark)">
{favicon}
<style>
{stylesheet()}
</style>
</head>
<body>

<header class="topbar">
  {icon_tag}
  <a class="name" href="{base}">{esc(ctx["title"])}</a>
  <span class="spacer"></span>
  <a class="btn btn-primary btn-sm" href="{esc(ctx["store_url"])}" target="_blank" rel="noopener">Get it</a>
</header>

<div class="hero">
  <h1>Page not found</h1>
  <p class="tagline">That page does not exist on the {esc(ctx["title"])} site.
  It may have been renamed, or the link that brought you here may be out of date.</p>
  <div class="cta">
    <a class="btn btn-primary" href="{base}">{esc(ctx["title"])} product page</a>
    <a class="btn" href="{esc(ctx["store_url"])}" target="_blank" rel="noopener">Store</a>
  </div>
</div>

<footer>
  <nav>
    <a href="{base}">Product page</a>
    <a href="{BRAND_URL}" target="_blank" rel="noopener">{BRAND}</a>
  </nav>
</footer>

</body>
</html>
"""
    (ctx["out_dir"] / "404.html").write_text(page, encoding="utf-8")


def build_page(plugin_dir: Path, out_dir: Path, encoder: Encoder, data=None) -> None:
    readme = plugin_dir / "README.md"
    if not readme.is_file():
        die(f"{plugin_dir.name}: no README.md")

    data = data or {}
    meta = read_meta(plugin_dir)
    readme_text = readme.read_text(encoding="utf-8")
    blocks, refs = parse_markdown(readme_text)
    title, intro, sections = organize(blocks)
    title = title or meta["product"]

    # Shared product data, unless the plugin's own site.json already said otherwise
    # (read_meta has merged site.json into meta already).
    extra = lookup_extra(data, [title, meta["product"], plugin_dir.name])
    if extra:
        meta.setdefault("storeUrl", (extra.get("link") or {}).get("url"))
        meta.setdefault("sameAs", extra.get("sameAs") or [])
        meta.setdefault("video", extra.get("video"))
        meta.setdefault("price", extra.get("price"))
        meta.setdefault("heroImage", extra.get("heroImage"))
        meta.setdefault("jsonLdIds", extra.get("jsonLdIds"))
        meta.setdefault("description", extra.get("description"))
    else:
        warn(f"{plugin_dir.name}: no entry in {PLUGIN_DATA.name} for \"{title}\"")

    # A README may link to this very page near the top; that link is noise once
    # the reader is already on it, and it must not be mistaken for the tagline.
    if meta.get("pages"):
        own_url = meta["pages"].rstrip("/")
        intro = [b for b in intro if not (b["type"] == "para" and own_url in b["text"])]

    # The intro sits above the fold: either the blocks before the first heading,
    # or an explicit Introduction/Description section.
    if not intro:
        for name in INTRO_SECTIONS:
            section = take_section(sections, name)
            if section:
                intro += section["blocks"]

    assign_slugs(sections)

    releases = take_section(sections, RELEASE_SECTION)
    version, date = latest_release(releases)
    version = version or meta.get("version")
    formats, systems, format_entries = format_details(find_section(sections, FORMATS_SECTION))
    modified = page_modified(plugin_dir, date)
    spec = parse_spec(find_section(sections, SPEC_SECTION))
    if not spec:
        warn(f"{plugin_dir.name}: no readable '{SPEC_SECTION}' — the summary will be thinner")

    # Release history goes after the manual, just before the repository notes.
    about = find_section(sections, ABOUT_SECTION)
    if releases:
        index = sections.index(about) if about else len(sections)
        sections.insert(index, releases)

    images = Images(plugin_dir, out_dir, encoder)
    hero_src = pick_hero(plugin_dir, meta)
    hero = images.register(hero_src) if hero_src else None
    renderer = Renderer(refs, images, skip_images=[hero_src] if hero else [])

    tagline_block = next((b for b in intro if b["type"] == "para"), None)
    if meta.get("tagline"):
        tagline, rest = meta["tagline"], intro
    else:
        tagline, leftover = split_tagline(tagline_block["text"] if tagline_block else "")
        rest = [b for b in intro if b is not tagline_block]
        if leftover:
            rest.insert(0, {"type": "para", "text": leftover})

    icon = None
    icon_src = plugin_dir / "packaging" / "icon.png"
    if icon_src.is_file():
        icon = "img/icon.png"

    videos = normalize_videos(meta.get("video"))
    video_html = render_video(videos, title, meta.get("pages")) if videos else ""

    store_url = meta.get("storeUrl") or DEFAULT_STORE_URL
    price = normalize_price(meta.get("price"), meta.get("currency", "USD"))
    description = meta.get("description") or seo_description(title, tagline)

    # The FAQ sits after the manual and before the release history: the manual is
    # what a reader came for, and the questions are what someone arriving from a
    # search still wants answered once they have skimmed it.
    faq = build_faq(title, description, format_entries, price, store_url, meta.get("repo"), spec)
    faq_html = render_faq(faq)
    faq_anchor = releases or about

    # The summary leads the page: a reader deciding whether this instrument fits
    # wants the formats, the price and the sample format before the manual, and
    # so does anything reading the page to answer a question about it.
    glance = build_glance(title, format_entries, systems, version, date, price, spec)
    glance_html = render_glance(glance)

    body = [glance_html] if glance_html else []
    if video_html:
        body.append(video_html)
    for section in sections:
        if section is faq_anchor and faq_html:
            body.append(faq_html)
        content = (
            split_releases(section, renderer)
            if section is releases
            else renderer.blocks(section["blocks"])
        )
        body.append(
            f'<section id="{section["slug"]}">'
            f'<h2><a class="anchor" href="#{section["slug"]}" aria-hidden="true">#</a>'
            f'{renderer.inline(section["title"])}</h2>{content}</section>'
        )
    if faq_html and not faq_anchor:
        body.append(faq_html)

    faq_link = '<li><a href="#faq">Frequently asked questions</a></li>' if faq_html else ""
    toc = ['<li><a href="#at-a-glance">At a glance</a></li>'] if glance_html else []
    if video_html:
        toc.append('<li><a href="#demo-video">Video</a></li>')
    for section in sections:
        if section is faq_anchor and faq_link:
            toc.append(faq_link)
        subs = "" if section is releases else "".join(
            f'<li><a href="#{s["slug"]}">{html.escape(s["text"])}</a></li>'
            for s in section_subheadings(section)
        )
        toc.append(
            f'<li><a href="#{section["slug"]}">{html.escape(strip_markdown(section["title"]))}</a>'
            + (f"<ul>{subs}</ul>" if subs else "")
            + "</li>"
        )
    if faq_link and not faq_anchor:
        toc.append(faq_link)

    product_page, product_page_no = product_pages(meta)
    more_html = render_more(sibling_plugins(plugin_dir, data), images)

    # Images are written before the HTML so fallback dimensions are known.
    images.emit()
    images.prune()

    out_dir.mkdir(parents=True, exist_ok=True)
    if icon:
        encoder.icon(icon_src, out_dir / "img" / "icon.png")
        images.written.add("icon.png")

    pages = meta.get("pages")
    ids = json_ld_ids(meta, pages, plugin_dir)
    html_text = page_html(
        title=title,
        product=meta["product"],
        tagline=tagline,
        intro_rest=renderer.blocks(rest),
        description=description,
        page_title=seo_title(title, formats),
        version=version,
        date=date,
        formats=formats,
        systems=systems,
        hero=hero,
        hero_alt=hero_alt(meta, readme_text, hero_src, title),
        icon=icon,
        repo=meta.get("repo"),
        owner=meta.get("owner"),
        slug=meta.get("slug"),
        pages=pages,
        store_url=store_url,
        product_page=product_page, product_page_no=product_page_no,
        price=price,
        toc="".join(toc),
        body="\n".join(body),
        more=more_html,
        video=meta.get("video"),
        json_ld=structured_data(
            title=title, description=description, pages=pages, store_url=store_url,
            version=version, date=date, systems=systems, hero=hero, images=images,
            repo=meta.get("repo"), price=price, ids=ids, tagline=tagline,
            same_as=meta.get("sameAs") or [], video=meta.get("video"), faq=faq,
            spec=spec, format_entries=format_entries, modified=modified,
            release_slug=releases["slug"] if releases else None,
        ),
    )
    (out_dir / "index.html").write_text(html_text, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    # The stylesheet is inlined into each page now, so the copy that used to sit
    # here is dead weight and, worse, a second stylesheet that could go stale.
    stale_css = out_dir / "style.css"
    if stale_css.is_file():
        stale_css.unlink()

    # Written after the page, because the image map it rewrites paths against is
    # only complete once everything has been rendered and emitted.
    if pages:
        (out_dir / "llms.txt").write_text(
            markdown_twin(
                {
                    "title": title, "description": description, "pages": pages,
                    "glance": glance, "faq": faq, "store_url": store_url,
                    "product_page": product_page, "repo": meta.get("repo"),
                    "videos": videos, "images": images,
                },
                readme_text,
            ),
            encoding="utf-8",
        )

    # Each video gets its own watch page; the product page only links to them.
    if pages:
        build_404_page({
            "title": title, "pages": pages, "out_dir": out_dir,
            "store_url": store_url, "icon": icon,
        })
        for index, video in enumerate(videos):
            build_watch_page(video, index, {
                "title": title, "tagline": tagline, "pages": pages, "out_dir": out_dir,
                "store_url": store_url, "price": price, "version": version,
                "formats": formats, "icon": icon, "ids": ids, "modified": modified,
                "cta_label": f"Download {title}" if price and float(price["minimum"]) == 0
                else f"Get {title}",
            })
        prune_watch_pages(out_dir, len(videos))

    if pages:
        (out_dir / "sitemap.xml").write_text(
            sitemap_xml(pages, modified, videos, title), encoding="utf-8"
        )
        (out_dir / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\n\nSitemap: {pages}sitemap.xml\n", encoding="utf-8"
        )


def prune_watch_pages(out_dir: Path, count: int) -> None:
    """Drop watch pages left over from a video that was removed from the data."""
    video_dir = out_dir / "video"
    if not video_dir.is_dir():
        return
    if count == 0:
        shutil.rmtree(video_dir)
        return
    for child in video_dir.iterdir():
        if child.is_dir() and child.name.isdigit() and int(child.name) > count:
            shutil.rmtree(child)


def seo_title(title: str, formats) -> str:
    """Keyword-bearing <title>: the product first, then what it actually is."""
    short = [f.replace(" application", "").strip() for f in formats][:3]
    what = ", ".join(short[:-1]) + " & " + short[-1] if len(short) > 1 else (short[0] if short else "")
    lead = f"{title}: {what} sample instrument" if what else f"{title}: sample instrument"
    return f"{lead} | {BRAND}"


def seo_description(title: str, tagline: str) -> str:
    text = strip_markdown(tagline)
    if text and title.lower() not in text.lower():
        text = f"{title}: {text}"
    return summarize(text or f"{title}, a sample instrument by {BRAND}.")



def product_pages(meta):
    """(English, Norwegian) pages for the instrument on dehlimusikk.no, from sameAs.

    Deliberately linked rather than declared as hreflang alternates. hreflang
    says two URLs are the same content in another language and is only honoured
    when both sides point at each other. Neither is true here: this page carries
    the whole manual, the pages on dehlimusikk.no do not, and those two already
    form their own no/en pair, so they will never point back at this one. A link
    tells a Norwegian reader where to go without making a claim that is false.
    """
    urls = [u for u in (meta.get("sameAs") or []) if "dehlimusikk.no/" in u and "/products/" in u]
    english = next((u for u in urls if "/en/products/" in u), None)
    norwegian = next((u for u in urls if "/en/products/" not in u), None)
    return english or norwegian, norwegian if english else None


def json_ld_ids(meta, pages, plugin_dir: Path):
    """The @ids that tie these pages to the same entities as dehlimusikk.no.

    They come from the data file so both sites can be kept in step by hand, but
    the website id has to be this page's real URL or the entity points nowhere,
    so a disagreement with the repository's actual Pages URL is reported.
    """
    ids = dict(meta.get("jsonLdIds") or {})
    derived = f"{pages}#website" if pages else None
    if derived and ids.get("website") and ids["website"] != derived:
        warn(
            f"{plugin_dir.name}: jsonLdIds.website is {ids['website']} but this "
            f"repository publishes at {pages} — using {derived}"
        )
        ids["website"] = derived
    ids.setdefault("website", derived)
    ids.setdefault("product", (pages + "#software") if pages else None)
    return ids


def author_nodes():
    return [
        {"@type": "Person", "@id": AUTHOR_ID, "name": AUTHOR_NAME, "url": BRAND_URL},
        {"@type": "Organization", "@id": PUBLISHER_ID, "name": BRAND, "url": BRAND_URL},
    ]


def site_root(pages: str) -> str:
    """The root of the domain this page is published on, e.g. the user site that
    every product site sits under as a project site."""
    parts = urllib.parse.urlsplit(pages)
    return f"{parts.scheme}://{parts.netloc}/"


def breadcrumb_node(identifier: str, trail):
    """A trail of (name, url) pairs, so results show the path rather than a URL.

    Every step stays on this domain. A breadcrumb states where the page sits in
    the hierarchy it is published in, so opening the trail on another site was
    describing a position this page does not occupy, and search engines drop a
    trail that wanders off the host rather than showing it.
    """
    return {
        "@type": "BreadcrumbList",
        "@id": identifier,
        "itemListElement": [
            {"@type": "ListItem", "position": n, "name": name, "item": url}
            for n, (name, url) in enumerate(trail, start=1)
        ],
    }


def webpage_node(ctx):
    """The product page itself. Without it the graph jumps from the site to the
    instrument and never says what this particular page is about."""
    node = {
        "@type": "WebPage",
        "@id": ctx["pages"],
        "url": ctx["pages"],
        "name": ctx["title"],
        "description": ctx["description"],
        "inLanguage": "en",
        "isPartOf": {"@id": ctx["ids"]["website"]},
        "mainEntity": {"@id": ctx["ids"]["product"]},
        "breadcrumb": {"@id": ctx["pages"] + "#breadcrumb"},
    }
    # The FAQ is a part of this page, not the whole of it: typing the product
    # page itself as an FAQPage would claim the manual is a list of questions.
    if ctx.get("faq"):
        node["hasPart"] = {"@id": ctx["pages"] + "#faq"}
    if ctx["hero"]:
        image = {"@type": "ImageObject", "url": ctx["pages"] + ctx["hero"]["url"]}
        size = ctx["hero"]["size"]
        if size:
            image["width"], image["height"] = size
        node["primaryImageOfPage"] = image
    if ctx.get("modified"):
        node["dateModified"] = ctx["modified"]
    return node


def website_node(ctx):
    """The GitHub Pages site itself, as distinct from the instrument it documents."""
    node = {
        "@type": "WebSite",
        "@id": ctx["ids"]["website"],
        "name": ctx["title"],
        "url": ctx["pages"],
        "description": (
            f"Product page and documentation for {ctx['title']}, "
            f"a sample instrument by {BRAND}."
        ),
        "inLanguage": "en",
        "author": {"@id": AUTHOR_ID},
        "publisher": {"@id": PUBLISHER_ID},
        "license": CONTENT_LICENSE,
        "about": {"@id": ctx["ids"]["product"]},
    }
    if ctx.get("repo"):
        node["sameAs"] = ctx["repo"]
    return node


def software_requirements(entries) -> str:
    """What a buyer needs besides the operating system, from the formats list.

    operatingSystem already carries the platforms, so this is the other half:
    a host for the plugin formats, the Decent Sampler application for the
    library, and nothing at all for the standalone build.
    """
    names = [e["name"] for e in entries]
    hosted = [n for n in names if n in ("VST3", "AU", "AAX", "CLAP", "VST")]
    # Alternatives, not prerequisites: one format or the other, never both.
    parts = []
    if hosted:
        parts.append(f"a host that loads {' or '.join(hosted)} for the plugin version")
    if any(DECENT_SAMPLER_NAME in n.lower() for n in names):
        parts.append("the free Decent Sampler application for the sample library version")
    if not parts:
        return ""
    sentence = "Requires " + ", or ".join(parts) + "."
    if any("standalone" in n.lower() for n in names):
        sentence += " The standalone application needs neither."
    return sentence


def structured_data(**ctx) -> str:
    """schema.org SoftwareApplication — the rich-result payload for the page."""
    if not ctx["pages"]:
        return ""
    pages = ctx["pages"]
    # The instrument is identified by its entry on dehlimusikk.no, so this page,
    # that site and the store all describe one and the same entity.
    entity = {
        # Also a Product, so the offer below is read by the shopping surfaces as
        # well as the software ones. It is both, and saying so costs nothing.
        "@type": ["SoftwareApplication", "Product"],
        "@id": ctx["ids"]["product"],
        "name": ctx["title"],
        "description": ctx["description"],
        "url": pages,
        "applicationCategory": "MultimediaApplication",
        "applicationSubCategory": "Sample library / virtual instrument",
        # Where to get it is stated once, by offers.url below: downloadUrl means
        # a link to the binary itself, which a store page is not.
        "author": {"@id": AUTHOR_ID},
        "publisher": {"@id": PUBLISHER_ID},
    }
    if ctx["systems"]:
        entity["operatingSystem"] = ", ".join(ctx["systems"])
    if ctx["version"]:
        entity["softwareVersion"] = ctx["version"]
    requirements = software_requirements(ctx.get("format_entries") or [])
    if requirements:
        entity["softwareRequirements"] = requirements
    # The space the audio takes, which is the figure a buyer needs. Not fileSize:
    # that means the size of the package itself, and the samples ship losslessly
    # compressed, so the download is smaller than the audio it unpacks to.
    if (ctx.get("spec") or {}).get("size"):
        entity["storageRequirements"] = ctx["spec"]["size"]
    if ctx.get("release_slug"):
        entity["releaseNotes"] = pages + "#" + ctx["release_slug"]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ctx["date"] or ""):
        entity["datePublished"] = ctx["date"]
    if ctx["hero"]:
        entity["image"] = pages + ctx["hero"]["url"]
    shots = [
        pages + entry["url"]
        for entry in ctx["images"].entries
        if "/screenshots-" in "/" + entry["url"]
    ]
    if shots:
        entity["screenshot"] = shots
    same_as = list(ctx.get("same_as") or [])
    if ctx["repo"] and ctx["repo"] not in same_as:
        same_as.append(ctx["repo"])
    if same_as:
        entity["sameAs"] = same_as
    price = ctx.get("price")
    offer = {"@type": "Offer", "url": ctx["store_url"], "availability": "https://schema.org/InStock"}
    if price:
        # Google reads offers.price; the store is pay what you want, so the
        # minimum is also published as a minPrice so the figure is not read as
        # a fixed one.
        offer["price"] = price["minimum"]
        offer["priceCurrency"] = price["currency"]
        if price["payWhatYouWant"]:
            offer["priceSpecification"] = {
                "@type": "PriceSpecification",
                "minPrice": price["minimum"],
                "priceCurrency": price["currency"],
                "description": price_note(price).rstrip("."),
            }
        if float(price["minimum"]) == 0:
            entity["isAccessibleForFree"] = True
    entity["offers"] = offer

    # The demo videos are described on their own watch pages, not here: a product
    # page is not a watch page, and Google will not index a video that is only a
    # click-to-load facade in the markup.
    crumbs = breadcrumb_node(
        pages + "#breadcrumb", [(AUTHOR_NAME, site_root(pages)), (ctx["title"], pages)]
    )
    graph = [website_node(ctx), webpage_node(ctx), crumbs, entity] + author_nodes()
    faq = faq_node(ctx.get("faq"), pages, ctx["title"])
    if faq:
        graph.append(faq)
    return json.dumps(
        {"@context": "https://schema.org", "@graph": graph}, indent=2, ensure_ascii=False
    )


def sitemap_xml(pages: str, lastmod, videos=(), title: str = "") -> str:
    """The product page plus one entry per watch page, the latter carrying the
    video sitemap extension Google asks for when you want video indexed."""
    stamp = f"\n    <lastmod>{lastmod}</lastmod>" if lastmod else ""
    entries = [f"  <url>\n    <loc>{pages}</loc>{stamp}\n  </url>"]

    for index, video in enumerate(videos):
        ident = youtube_id(video["contentUrl"])
        name = pick_language(video.get("name")) or f"{title} demo"
        description = pick_language(video.get("description")) or f"A demonstration of {title}."
        published = upload_datetime(video.get("uploadDate"))
        _, seconds = video_duration(video, f"{title} video")
        # The watch pages are regenerated from the same sources as the product
        # page, so they changed when it did and carry the same lastmod.
        lines = [
            f"  <url>\n    <loc>{watch_url(pages, index)}</loc>{stamp}",
            "    <video:video>",
            f"      <video:thumbnail_loc>https://i.ytimg.com/vi/{ident}/hqdefault.jpg</video:thumbnail_loc>",
            f"      <video:title>{html.escape(name)}</video:title>",
            f"      <video:description>{html.escape(description)}</video:description>",
            f"      <video:player_loc>https://www.youtube.com/embed/{ident}</video:player_loc>",
        ]
        if seconds:
            lines.append(f"      <video:duration>{seconds}</video:duration>")
        if published:
            lines.append(f"      <video:publication_date>{published}</video:publication_date>")
        lines += ["    </video:video>", "  </url>"]
        entries.append("\n".join(lines))

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
        '        xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">\n'
        + "\n".join(entries)
        + "\n</urlset>\n"
    )


def split_tagline(text: str, limit: int = 200):
    """A hero reads badly as a wall of text: keep the opening sentence up top
    and push the rest of the paragraph below the buttons."""
    text = text.strip()
    if len(text) <= limit:
        return text, ""
    parts = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
    if len(parts) == 2 and len(parts[0]) >= 40:
        return parts[0], parts[1]
    return text, ""


def summarize(text: str, limit: int = 155) -> str:
    text = strip_markdown(text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def page_html(**ctx) -> str:
    esc = lambda value: html.escape(str(value), quote=True)  # noqa: E731

    badges = []
    if ctx.get("price"):
        badges.append(f'<span class="badge badge-price">{esc(price_badge(ctx["price"]))}</span>')
    if ctx["version"]:
        label = f"Version {ctx['version']}"
        badges.append(f'<span class="badge">{esc(label)}</span>')
    if ctx["date"]:
        badges.append(f'<span class="badge">{esc(ctx["date"])}</span>')
    for fmt in ctx["formats"]:
        badges.append(f'<span class="badge">{esc(fmt)}</span>')

    hero_shot = preload = ""
    if ctx["hero"]:
        size = ctx["hero"]["size"] or (0, 0)
        dims = f' width="{size[0]}" height="{size[1]}"' if size[0] else ""
        hero_shot = (
            f'<div class="hero-shot"><img src="{ctx["hero"]["url"]}" '
            f'alt="{esc(ctx["hero_alt"])}" fetchpriority="high" decoding="async"{dims}></div>'
        )
        # The hero image is the LCP element; start it before the CSS resolves.
        preload = f'<link rel="preload" as="image" href="{ctx["hero"]["url"]}" fetchpriority="high">'

    icon_tag = f'<img src="{ctx["icon"]}" alt="" class="hero-icon">' if ctx["icon"] else ""
    topbar_icon = f'<img src="{ctx["icon"]}" alt="">' if ctx["icon"] else ""
    favicon = f'<link rel="icon" href="{ctx["icon"]}">' if ctx["icon"] else ""
    canonical = f'<link rel="canonical" href="{esc(ctx["pages"])}">' if ctx.get("pages") else ""
    # The same page as markdown, for anything that would rather not parse HTML.
    markdown_link = (
        '<link rel="alternate" type="text/markdown" href="llms.txt" '
        f'title="{esc(ctx["title"])} as markdown">'
        if ctx.get("pages")
        else ""
    )
    og_url = f'<meta property="og:url" content="{esc(ctx["pages"])}">' if ctx.get("pages") else ""
    og_image = twitter_image = ""
    if ctx.get("pages") and ctx["hero"]:
        size = ctx["hero"]["size"] or (0, 0)
        image_url = ctx["pages"] + ctx["hero"]["url"]
        og_image = (
            f'<meta property="og:image" content="{esc(image_url)}">\n'
            f'<meta property="og:image:alt" content="{esc(ctx["hero_alt"])}">'
        )
        if size[0]:
            og_image += (
                f'\n<meta property="og:image:width" content="{size[0]}">'
                f'\n<meta property="og:image:height" content="{size[1]}">'
            )
        # Twitter falls back to og:image, but only some readers of these tags do.
        twitter_image = (
            f'<meta name="twitter:image" content="{esc(image_url)}">\n'
            f'<meta name="twitter:image:alt" content="{esc(ctx["hero_alt"])}">'
        )

    # og:type is product, so the price belongs in the card as well as in the
    # structured data. Pay what you want has no OG equivalent, so this is the
    # minimum, which is what offers.price says too.
    og_product = ""
    if ctx.get("price"):
        og_product = (
            f'<meta property="product:price:amount" content="{esc(ctx["price"]["minimum"])}">\n'
            f'<meta property="product:price:currency" content="{esc(ctx["price"]["currency"])}">\n'
            '<meta property="og:availability" content="instock">'
        )
    json_ld = (
        f'<script type="application/ld+json">\n{ctx["json_ld"]}\n</script>' if ctx.get("json_ld") else ""
    )
    # og:video belongs on the watch page, where the player actually is.
    og_video = (
        '<link rel="preconnect" href="https://i.ytimg.com">'
        if normalize_videos(ctx.get("video"))
        else ""
    )
    # Star and follow both need a signed-in GitHub session, so send visitors
    # through the login page and straight back to where they were headed.
    github_cta = ""
    if ctx.get("slug") and ctx.get("owner"):
        def login_link(path: str, icon: str, label: str) -> str:
            target = urllib.parse.quote("/" + path, safe="")
            return (
                f'<a class="btn btn-sm btn-ghost" href="https://github.com/login?return_to={target}"'
                ' target="_blank" rel="noopener">'
                f'<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">'
                f'<path d="{icon}"/></svg>{esc(label)}</a>'
            )

        github_cta = (
            '<div class="github-cta">'
            + login_link(ctx["slug"], ICON_STAR, "Star the repo")
            + login_link(ctx["owner"], ICON_GITHUB, f"Follow @{ctx['owner']}")
            + "</div>"
        )

    price_line = ""
    cta_label = f"Get {ctx['title']}"
    if ctx.get("price"):
        price_line = f'<p class="price-note">{esc(price_note(ctx["price"]))}</p>'
        if float(ctx["price"]["minimum"]) == 0:
            cta_label = f"Download {ctx['title']}"
    ctx["cta_label"] = cta_label

    repo_link = (
        f'<a class="btn" href="{esc(ctx["repo"])}" target="_blank" rel="noopener">View on GitHub</a>'
        if ctx["repo"]
        else ""
    )
    product_page_link = (
        f'<a href="{esc(ctx["product_page"])}" target="_blank" rel="noopener">'
        f'{esc(ctx["title"])} at {BRAND}</a>'
        if ctx.get("product_page")
        else ""
    )
    # hreflang and lang on the anchor are advisory: they say the page at the end
    # of this link is in Norwegian. That is different from rel="alternate", which
    # would claim it is this page translated, and it is not.
    norwegian_link = (
        f'<a href="{esc(ctx["product_page_no"])}" hreflang="no" lang="no" '
        'target="_blank" rel="noopener">På norsk</a>'
        if ctx.get("product_page_no")
        else ""
    )
    repo_footer = (
        f'<a href="{esc(ctx["repo"])}" target="_blank" rel="noopener">Source on GitHub</a>'
        if ctx["repo"]
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(ctx["page_title"])}</title>
<meta name="description" content="{esc(ctx["description"])}">
<meta name="author" content="{BRAND}">
<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">
<meta name="theme-color" content="#a35a2a" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#17161a" media="(prefers-color-scheme: dark)">
{canonical}
{markdown_link}
<meta property="og:type" content="product">
<meta property="og:site_name" content="{BRAND}">
<meta property="og:locale" content="en_GB">
<meta property="og:title" content="{esc(ctx["page_title"])}">
<meta property="og:description" content="{esc(ctx["description"])}">
{og_url}
{og_image}
{og_product}
{og_video}
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(ctx["page_title"])}">
<meta name="twitter:description" content="{esc(ctx["description"])}">
{twitter_image}
{favicon}
{preload}
<style>
{stylesheet()}
</style>
{json_ld}
</head>
<body>

<header class="topbar">
  {topbar_icon}
  <a class="name" href="#">{esc(ctx["title"])}</a>
  <span class="spacer"></span>
  <a class="btn btn-primary btn-sm" href="{esc(ctx["store_url"])}" target="_blank" rel="noopener">Get it</a>
</header>

<div class="hero">
  {icon_tag}
  <h1>{esc(ctx["title"])}</h1>
  <p class="tagline">{ctx["tagline"] and esc(ctx["tagline"]) or ""}</p>
  <div class="badges">{"".join(badges)}</div>
  <div class="cta">
    <a class="btn btn-primary" href="{esc(ctx["store_url"])}" target="_blank" rel="noopener">{esc(ctx["cta_label"])}</a>
    {repo_link}
  </div>
  {price_line}
  {github_cta}
  <div class="intro-more">{ctx["intro_rest"]}</div>
</div>

{hero_shot}

<div class="layout">
  <nav class="toc" aria-labelledby="toc-title">
    <p class="toc-title" id="toc-title">On this page</p>
    <ul>{ctx["toc"]}</ul>
  </nav>
  <main>
{ctx["body"]}
  </main>
</div>

{ctx["more"]}

<footer>
  {topbar_icon}
  <nav>
    {product_page_link}
    {norwegian_link}
    <a href="{esc(ctx["store_url"])}" target="_blank" rel="noopener">Store</a>
    <a href="{BRAND_URL}" target="_blank" rel="noopener">{BRAND}</a>
    <a href="{DECENT_SAMPLER_URL}" target="_blank" rel="noopener">Decent Sampler</a>
    {repo_footer}
  </nav>
  <p>{esc(ctx["title"])} is a sample instrument by {BRAND}.<br>
  This page is generated from the repository README.</p>
</footer>

<script>
// Click to load: nothing is requested from YouTube until the reader asks for it.
(function () {{
  document.querySelectorAll('.video[data-youtube]').forEach(function (box) {{
    var button = box.querySelector('.video-play');
    if (!button) return;
    button.addEventListener('click', function () {{
      var frame = document.createElement('iframe');
      frame.src = 'https://www.youtube-nocookie.com/embed/' + box.dataset.youtube +
                  '?autoplay=1&rel=0';
      frame.title = button.getAttribute('aria-label') || 'Video';
      frame.allow = 'accelerometer; autoplay; encrypted-media; picture-in-picture';
      frame.allowFullscreen = true;
      frame.loading = 'lazy';
      box.replaceChildren(frame);
    }});
  }});
}})();

// Highlight the section the reader is in, in the sidebar.
(function () {{
  var links = {{}};
  document.querySelectorAll('.toc a').forEach(function (a) {{
    links[a.getAttribute('href').slice(1)] = a;
  }});
  var targets = document.querySelectorAll('main section, main h3[id]');
  if (!targets.length || !window.IntersectionObserver) return;
  var current = null;
  var observer = new IntersectionObserver(function (entries) {{
    entries.forEach(function (entry) {{
      if (!entry.isIntersecting) return;
      var link = links[entry.target.id];
      if (!link || link === current) return;
      if (current) current.classList.remove('current');
      link.classList.add('current');
      current = link;
    }});
  }}, {{ rootMargin: '-20% 0px -70% 0px' }});
  targets.forEach(function (t) {{ observer.observe(t); }});
}})();
</script>

</body>
</html>
"""


# ── entry point ──────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate a plugin's GitHub Pages site from its README.")
    parser.add_argument("plugins", nargs="+", help="plugin directories (e.g. omni-84-plugin)")
    parser.add_argument(
        "--format", choices=("auto", "webp", "jpeg"), default="auto",
        help="screenshot encoding (default: webp when an encoder is available, else jpeg)",
    )
    parser.add_argument("--out", default="docs", help="output folder inside each plugin repo (default: docs)")
    parser.add_argument(
        "--data", default=str(PLUGIN_DATA),
        help="shared product data: store links, sameAs profiles and demo videos",
    )
    parser.add_argument(
        "--allow-unoptimized", action="store_true",
        help="write full size copies when no image tool is installed (they are "
             "tens of megabytes; the default is to stop instead)",
    )
    args = parser.parse_args(argv)

    encoder = Encoder(args.format)
    if not encoder.tool and not args.allow_unoptimized:
        die(
            "no image tool found (cwebp, sips, ImageMagick or Pillow).\n"
            "  Copying the screenshots at full size would add tens of megabytes to\n"
            "  every plugin repo, so nothing was written. Install one:\n"
            "      brew install webp        # macOS, gives cwebp\n"
            "      sudo apt install webp    # Debian/Ubuntu\n"
            "  (sips ships with macOS, so this should not happen there.)\n"
            "  Pass --allow-unoptimized to write the copies anyway."
        )
    info(f"Encoding screenshots as {encoder.describe()}")
    data = load_plugin_data(Path(args.data))

    for name in args.plugins:
        plugin_dir = (ROOT / name).resolve() if not Path(name).is_absolute() else Path(name)
        if not plugin_dir.is_dir():
            die(f"no such plugin directory: {name}")
        out_dir = plugin_dir / args.out
        build_page(plugin_dir, out_dir, encoder, data)
        info(f"{plugin_dir.name} → {out_dir.relative_to(ROOT)}/index.html")

    build_root_sitemap(data)
    build_root_llms(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
