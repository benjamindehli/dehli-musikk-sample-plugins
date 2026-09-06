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
AUTHOR_ID = "https://musicbrainz.org/artist/56639e59-2bb5-40bd-9d5a-97d964298b6f"
AUTHOR_NAME = "Benjamin Dehli"
PUBLISHER_ID = BRAND_URL
# Profiles that describe the label itself, as opposed to a single product. Each
# product's own profiles live in plugin-data.json under "sameAs".
PUBLISHER_SAME_AS = ["https://www.kvraudio.com/developer/dehli-musikk"]
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
    """(format names, operating systems) from the 'Included formats' list.

    The list reads "VST3 (macOS)", "Decent Sampler (macOS, Windows and Linux)",
    which gives both the badges and an accurate schema.org operatingSystem.
    """
    if not section:
        return [], []
    names, systems = [], []
    for block in section["blocks"]:
        if block["type"] != "list":
            continue
        for item in block["items"]:
            raw = strip_markdown(item["text"])
            name = raw.split(" (")[0].replace(" application", "").strip()
            if name and name not in names:
                names.append(name)
            inside = re.search(r"\(([^)]+)\)", raw)
            if inside:
                for system in re.split(r",|\band\b", inside.group(1)):
                    system = system.strip()
                    if system and system not in systems:
                        systems.append(system)
    return names, systems


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
    """Normalize a product name so "MaskinTrommer", "Maskintrommer" and the
    directory "maskintrommer-plugin" all resolve to the same entry."""
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
        return "Free — pay what you want." if price["payWhatYouWant"] else "Free."
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


def youtube_id(url: str):
    match = YOUTUBE_ID_RE.search(url or "")
    return match.group(1) if match else None


def pick_language(value, language: str = "en"):
    """The data file carries {"en": ..., "no": ...}; these pages are English."""
    if isinstance(value, dict):
        return value.get(language) or next(iter(value.values()), "")
    return value or ""


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

    try:
        url = subprocess.run(
            ["git", "-C", str(plugin_dir), "remote", "get-url", "origin"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        match = re.search(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?$", url)
        if match:
            owner, repo = match.group(1), match.group(2)
            meta["owner"] = owner
            meta["slug"] = f"{owner}/{repo}"
            meta["repo"] = f"https://github.com/{owner}/{repo}"
            meta["pages"] = f"https://{owner}.github.io/{repo}/"
    except (subprocess.CalledProcessError, OSError):
        warn(f"{plugin_dir.name}: no git remote — GitHub links will be omitted")

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
        label = html.escape(f"Play video: {name}", quote=True)
        players.append(
            '<figure class="video-item">'
            + f'<div class="video" data-youtube="{ident}">'
            f'<button class="video-play" type="button" aria-label="{label}">'
            f'<img src="https://i.ytimg.com/vi/{ident}/hqdefault.jpg" alt="" loading="lazy" '
            'width="480" height="360">'
            '<span class="play" aria-hidden="true"></span></button></div>'
            f'<figcaption class="video-caption"><strong>{html.escape(name)}</strong>'
            + (f" — {html.escape(description)}" if description and len(videos) > 1 else "")
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
        warn(f"{ctx['title']}: video has no usable uploadDate — Google requires it")

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
        },
        breadcrumb_node(url + "#breadcrumb", [
            (BRAND, BRAND_URL),
            (ctx["title"], ctx["pages"]),
            ("Video" if index == 0 else f"Video {index + 1}", url),
        ]),
        video_node,
    ] + author_nodes()

    icon_tag = f'<img src="{up}img/icon.png" alt="">' if ctx["icon"] else ""
    favicon = f'<link rel="icon" href="{up}img/icon.png">' if ctx["icon"] else ""

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
{favicon}
<link rel="stylesheet" href="{up}style.css">
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
  <p>{esc(name)} — a demonstration of {esc(ctx["title"])}, a sample instrument by {BRAND}.</p>
</footer>

</body>
</html>
"""
    directory = watch_dir(ctx["out_dir"], index)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(page, encoding="utf-8")


def build_root_sitemap(data) -> None:
    """A robots.txt and sitemap index for the user site, benjamindehli.github.io.

    A robots.txt only counts at the domain root, so the one each project site
    generates is never read. Publishing these two files from the user site's own
    repository is what makes all thirteen sitemaps discoverable from one place.
    Written for every plugin in the workspace, not only the ones being built, so
    a single plugin run cannot truncate the index.
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
    (out_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {origin}sitemap-index.xml\n", encoding="utf-8"
    )
    (out_dir / "README.md").write_text(
        "# Files for the user site\n\n"
        f"Generated by `./dmse site`. Copy them to the root of the `{origin.split('//')[1].rstrip('/')}`\n"
        "repository, the user site, and publish it through GitHub Pages.\n\n"
        "A `robots.txt` is only honoured at the root of a domain, so the one each plugin site\n"
        "generates at `/<Repo>/robots.txt` is never read by a crawler. These two files are read:\n"
        "the `robots.txt` points at the sitemap index, and the index lists the sitemap of every\n"
        "plugin site, so all of them are discoverable from one URL rather than thirteen manual\n"
        "submissions in Search Console.\n\n"
        "If that repository already has a `robots.txt`, merge the `Sitemap:` line into it rather\n"
        "than overwriting the file. Re-run `./dmse site` and copy them again whenever a plugin is\n"
        "added or removed.\n",
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
<link rel="stylesheet" href="{base}style.css">
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
    blocks, refs = parse_markdown(readme.read_text(encoding="utf-8"))
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
    formats, systems = format_details(find_section(sections, FORMATS_SECTION))

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

    body = [video_html] if video_html else []
    for section in sections:
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

    toc = ['<li><a href="#demo-video">Video</a></li>'] if video_html else []
    for section in sections:
        subs = "" if section is releases else "".join(
            f'<li><a href="#{s["slug"]}">{html.escape(s["text"])}</a></li>'
            for s in section_subheadings(section)
        )
        toc.append(
            f'<li><a href="#{section["slug"]}">{html.escape(strip_markdown(section["title"]))}</a>'
            + (f"<ul>{subs}</ul>" if subs else "")
            + "</li>"
        )

    store_url = meta.get("storeUrl") or DEFAULT_STORE_URL
    price = normalize_price(meta.get("price"), meta.get("currency", "USD"))
    description = meta.get("description") or seo_description(title, tagline)
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
        keywords=seo_keywords(title, formats),
        version=version,
        date=date,
        formats=formats,
        systems=systems,
        hero=hero,
        icon=icon,
        repo=meta.get("repo"),
        owner=meta.get("owner"),
        slug=meta.get("slug"),
        pages=pages,
        store_url=store_url,
        product_page=product_page_url(meta),
        price=price,
        toc="".join(toc),
        body="\n".join(body),
        more=more_html,
        video=meta.get("video"),
        json_ld=structured_data(
            title=title, description=description, pages=pages, store_url=store_url,
            version=version, date=date, systems=systems, hero=hero, images=images,
            repo=meta.get("repo"), price=price, ids=ids, tagline=tagline,
            same_as=meta.get("sameAs") or [], video=meta.get("video"),
        ),
    )
    (out_dir / "index.html").write_text(html_text, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    shutil.copy2(SITE_DIR / "style.css", out_dir / "style.css")

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
                "formats": formats, "icon": icon, "ids": ids,
                "cta_label": f"Download {title}" if price and float(price["minimum"]) == 0
                else f"Get {title}",
            })
        prune_watch_pages(out_dir, len(videos))

    if pages:
        lastmod = date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or "") else None
        (out_dir / "sitemap.xml").write_text(
            sitemap_xml(pages, lastmod, videos, title), encoding="utf-8"
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
    lead = f"{title} — {what} sample instrument" if what else f"{title} — sample instrument"
    return f"{lead} | {BRAND}"


def seo_description(title: str, tagline: str) -> str:
    text = strip_markdown(tagline)
    if text and title.lower() not in text.lower():
        text = f"{title}: {text}"
    return summarize(text or f"{title}, a sample instrument by {BRAND}.")


def seo_keywords(title: str, formats) -> str:
    words = [title, "sample library", "sample instrument", "virtual instrument"]
    words += [f.replace(" application", "") for f in formats]
    words += ["Decent Sampler", BRAND]
    seen = []
    for word in words:
        if word and word.lower() not in [s.lower() for s in seen]:
            seen.append(word)
    return ", ".join(seen)


def product_page_url(meta):
    """The instrument's page on dehlimusikk.no, taken from sameAs. These pages are
    English, so the English variant wins when both are listed."""
    urls = [u for u in (meta.get("sameAs") or []) if "dehlimusikk.no/" in u and "/products/" in u]
    english = [u for u in urls if "/en/products/" in u]
    return (english or urls or [None])[0]


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
        {
            "@type": "Organization",
            "@id": PUBLISHER_ID,
            "name": BRAND,
            "url": BRAND_URL,
            "sameAs": PUBLISHER_SAME_AS,
        },
    ]


def breadcrumb_node(identifier: str, trail):
    """A trail of (name, url) pairs, so results show the path rather than a URL."""
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
    if ctx["hero"]:
        image = {"@type": "ImageObject", "url": ctx["pages"] + ctx["hero"]["url"]}
        size = ctx["hero"]["size"]
        if size:
            image["width"], image["height"] = size
        node["primaryImageOfPage"] = image
    # The page is regenerated from the README, so a release is what changes it.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ctx["date"] or ""):
        node["dateModified"] = ctx["date"]
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


def structured_data(**ctx) -> str:
    """schema.org SoftwareApplication — the rich-result payload for the page."""
    if not ctx["pages"]:
        return ""
    pages = ctx["pages"]
    # The instrument is identified by its entry on dehlimusikk.no, so this page,
    # that site and the store all describe one and the same entity.
    entity = {
        "@type": "SoftwareApplication",
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
        pages + "#breadcrumb", [(BRAND, BRAND_URL), (ctx["title"], pages)]
    )
    graph = [website_node(ctx), webpage_node(ctx), crumbs, entity] + author_nodes()
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
        lines = [
            f"  <url>\n    <loc>{watch_url(pages, index)}</loc>",
            "    <video:video>",
            f"      <video:thumbnail_loc>https://i.ytimg.com/vi/{ident}/hqdefault.jpg</video:thumbnail_loc>",
            f"      <video:title>{html.escape(name)}</video:title>",
            f"      <video:description>{html.escape(description)}</video:description>",
            f"      <video:player_loc>https://www.youtube.com/embed/{ident}</video:player_loc>",
        ]
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
        alt = f'{ctx["title"]} plugin interface, showing its controls and on-screen keyboard'
        hero_shot = (
            f'<div class="hero-shot"><img src="{ctx["hero"]["url"]}" '
            f'alt="{esc(alt)}" fetchpriority="high" decoding="async"{dims}></div>'
        )
        # The hero image is the LCP element; start it before the CSS resolves.
        preload = f'<link rel="preload" as="image" href="{ctx["hero"]["url"]}" fetchpriority="high">'

    icon_tag = f'<img src="{ctx["icon"]}" alt="" class="hero-icon">' if ctx["icon"] else ""
    topbar_icon = f'<img src="{ctx["icon"]}" alt="">' if ctx["icon"] else ""
    favicon = f'<link rel="icon" href="{ctx["icon"]}">' if ctx["icon"] else ""
    canonical = f'<link rel="canonical" href="{esc(ctx["pages"])}">' if ctx.get("pages") else ""
    og_url = f'<meta property="og:url" content="{esc(ctx["pages"])}">' if ctx.get("pages") else ""
    og_image = ""
    if ctx.get("pages") and ctx["hero"]:
        size = ctx["hero"]["size"] or (0, 0)
        og_image = (
            f'<meta property="og:image" content="{esc(ctx["pages"] + ctx["hero"]["url"])}">\n'
            f'<meta property="og:image:alt" content="{esc(ctx["title"])} plugin interface">'
        )
        if size[0]:
            og_image += (
                f'\n<meta property="og:image:width" content="{size[0]}">'
                f'\n<meta property="og:image:height" content="{size[1]}">'
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
<meta name="keywords" content="{esc(ctx["keywords"])}">
<meta name="author" content="{BRAND}">
<meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">
<meta name="theme-color" content="#a35a2a" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#17161a" media="(prefers-color-scheme: dark)">
{canonical}
<meta property="og:type" content="product">
<meta property="og:site_name" content="{BRAND}">
<meta property="og:locale" content="en_GB">
<meta property="og:title" content="{esc(ctx["page_title"])}">
<meta property="og:description" content="{esc(ctx["description"])}">
{og_url}
{og_image}
{og_video}
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(ctx["page_title"])}">
<meta name="twitter:description" content="{esc(ctx["description"])}">
{favicon}
{preload}
<link rel="stylesheet" href="style.css">
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
