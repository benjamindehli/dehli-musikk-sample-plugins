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
import html
import json
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent
ROOT = SITE_DIR.parent

BRAND = "Dehli Musikk"
BRAND_URL = "https://www.dehlimusikk.no/"
DEFAULT_STORE_URL = "https://store.dehlimusikk.no/"
DECENT_SAMPLER_URL = "https://www.decentsamples.com/product/decent-sampler-plugin/"

MAX_IMAGE_WIDTH = 1200
PHOTO_QUALITY = 80
ICON_SIZE = 256

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
        if not self.tool:
            warn(
                "no image tool found (cwebp, sips, ImageMagick or Pillow) — "
                "screenshots will be copied at full size. Re-run on macOS, or "
                "install one, to optimize them."
            )

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
        self.by_src = {}
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

        stem = re.sub(r"[^a-z0-9]+", "-", f"{source.parent.name}-{source.stem}".lower()).strip("-")
        # Without an encoder the file is copied verbatim, so keep its real type.
        ext = self.encoder.ext if self.encoder.tool else source.suffix.lstrip(".").lower()
        entry = {
            "url": f"img/{stem}.{ext}",
            "path": self.out_dir / "img" / f"{stem}.{ext}",
            "source": source,
            "size": scaled_size(image_size(source), MAX_IMAGE_WIDTH),
        }
        self.by_src[key] = entry
        return entry

    def emit(self) -> None:
        img_dir = self.out_dir / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        for entry in self.by_src.values():
            if not entry:
                continue
            encoded = self.encoder.photo(entry["source"], entry["path"], MAX_IMAGE_WIDTH)
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

    def figure(self, src: str, alt: str, caption: str) -> str:
        entry = self.images.register(src)
        if not entry:
            return f"<p>{self.inline(caption or alt)}</p>"
        if src.strip() in self.skip_images:
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

def read_meta(plugin_dir: Path):
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


# ── page assembly ────────────────────────────────────────────────────────────

def build_page(plugin_dir: Path, out_dir: Path, encoder: Encoder) -> None:
    readme = plugin_dir / "README.md"
    if not readme.is_file():
        die(f"{plugin_dir.name}: no README.md")

    meta = read_meta(plugin_dir)
    blocks, refs = parse_markdown(readme.read_text(encoding="utf-8"))
    title, intro, sections = organize(blocks)
    title = title or meta["product"]

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

    body = []
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

    toc = []
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

    store_url = meta.get("storeUrl", DEFAULT_STORE_URL)
    description = meta.get("description") or seo_description(title, tagline)

    # Images are written before the HTML so fallback dimensions are known.
    images.emit()
    images.prune()

    out_dir.mkdir(parents=True, exist_ok=True)
    if icon:
        encoder.icon(icon_src, out_dir / "img" / "icon.png")
        images.written.add("icon.png")

    pages = meta.get("pages")
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
        pages=pages,
        store_url=store_url,
        toc="".join(toc),
        body="\n".join(body),
        json_ld=structured_data(
            title=title, description=description, pages=pages, store_url=store_url,
            version=version, date=date, systems=systems, hero=hero, images=images,
            repo=meta.get("repo"), price=meta.get("price"), currency=meta.get("currency", "USD"),
        ),
    )
    (out_dir / "index.html").write_text(html_text, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    shutil.copy2(SITE_DIR / "style.css", out_dir / "style.css")

    if pages:
        lastmod = date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or "") else None
        (out_dir / "sitemap.xml").write_text(sitemap_xml(pages, lastmod), encoding="utf-8")
        (out_dir / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\n\nSitemap: {pages}sitemap.xml\n", encoding="utf-8"
        )


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


def structured_data(**ctx) -> str:
    """schema.org SoftwareApplication — the rich-result payload for the page."""
    if not ctx["pages"]:
        return ""
    pages = ctx["pages"]
    entity = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "@id": pages + "#software",
        "name": ctx["title"],
        "description": ctx["description"],
        "url": pages,
        "applicationCategory": "MultimediaApplication",
        "applicationSubCategory": "Sample library / virtual instrument",
        "author": {"@type": "Organization", "name": BRAND, "url": BRAND_URL},
        "publisher": {"@type": "Organization", "name": BRAND, "url": BRAND_URL},
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
        for entry in ctx["images"].by_src.values()
        if entry and "/screenshots-" in "/" + entry["url"]
    ]
    if shots:
        entity["screenshot"] = shots
    if ctx["repo"]:
        entity["sameAs"] = [ctx["repo"]]
    if ctx["price"] is not None:
        entity["offers"] = {
            "@type": "Offer",
            "url": ctx["store_url"],
            "price": str(ctx["price"]),
            "priceCurrency": ctx["currency"],
            "availability": "https://schema.org/InStock",
        }
    else:
        # No price in site.json: still tell crawlers where it is sold.
        entity["offers"] = {
            "@type": "Offer",
            "url": ctx["store_url"],
            "availability": "https://schema.org/InStock",
        }
    return json.dumps(entity, indent=2, ensure_ascii=False)


def sitemap_xml(pages: str, lastmod) -> str:
    stamp = f"\n    <lastmod>{lastmod}</lastmod>" if lastmod else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url>\n    <loc>{pages}</loc>{stamp}\n  </url>\n"
        "</urlset>\n"
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
    repo_link = (
        f'<a class="btn" href="{esc(ctx["repo"])}" target="_blank" rel="noopener">View on GitHub</a>'
        if ctx["repo"]
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
    <a class="btn btn-primary" href="{esc(ctx["store_url"])}" target="_blank" rel="noopener">Get {esc(ctx["title"])}</a>
    {repo_link}
  </div>
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

<footer>
  {topbar_icon}
  <nav>
    <a href="{esc(ctx["store_url"])}" target="_blank" rel="noopener">Store</a>
    <a href="{BRAND_URL}" target="_blank" rel="noopener">{BRAND}</a>
    <a href="{DECENT_SAMPLER_URL}" target="_blank" rel="noopener">Decent Sampler</a>
    {repo_footer}
  </nav>
  <p>{esc(ctx["title"])} is a sample instrument by {BRAND}.<br>
  This page is generated from the repository README.</p>
</footer>

<script>
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
    args = parser.parse_args(argv)

    encoder = Encoder(args.format)
    info(f"Encoding screenshots as {encoder.describe()}")

    for name in args.plugins:
        plugin_dir = (ROOT / name).resolve() if not Path(name).is_absolute() else Path(name)
        if not plugin_dir.is_dir():
            die(f"no such plugin directory: {name}")
        out_dir = plugin_dir / args.out
        build_page(plugin_dir, out_dir, encoder)
        info(f"{plugin_dir.name} → {out_dir.relative_to(ROOT)}/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
