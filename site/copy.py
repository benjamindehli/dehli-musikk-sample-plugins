#!/usr/bin/env python3
"""Write each plugin's product description for the sites that are not built
from the repo, so they are pasted rather than retyped.

    ./dmse copy subc
    ./dmse copy all
    ./dmse copy all --dehlimusikk ~/path/to/products.json

A plugin's description is Description/en.md in its repository, with its
Norwegian version in Description/no.md: a few paragraphs and a "## Features"
list, written by hand. From it and the README this writes, per plugin, into
build/copy/<plugin>/:

  gumroad.html  the store page: the description, then the README without its
                title, the link to the product page, the technical
                specification (the store's editor has no tables) and the notes
                about the repository. Open it, press "Copy description" and
                paste it into the store's editor below the video, which a paste
                cannot carry. Images point at the files in the plugin's public
                GitHub repository.
  kvr.html      the HTML block for the KVR product page: the video, the
                description and a link to the product page.

With --dehlimusikk, the content.en and content.no of each plugin's entry in
that file (matched by title) are replaced with the description as plain lines,
with equipment named in the README linked to its page on the site. Every other
field and every other product is left as it is.

Only the Python standard library is used, so it runs on a stock macOS.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_site as site  # noqa: E402

ROOT = site.ROOT
OUT_DIR = ROOT / "build" / "copy"
LEFT_OUT = (site.SPEC_SECTION, site.ABOUT_SECTION)
SITE_URL = "https://www.dehlimusikk.no/"
GITHUB_RE = re.compile(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?/?$")


class RepoImages:
    """Images as the files in the plugin's GitHub repository."""

    def __init__(self, base: str):
        self.base = base

    def register(self, src: str):
        path = src.strip().lstrip("/")
        if not path or path.startswith("http"):
            return {"url": src.strip(), "size": None} if path else None
        return {"url": f"{self.base}/{path}", "size": None}


class StoreRenderer(site.Renderer):
    """The README in the plain shapes the store's editor keeps."""

    def __init__(self, refs, images):
        super().__init__(refs, images)

    def _link(self, label: str, href: str) -> str:
        return f'<a href="{html.escape(href, quote=True)}">{label}</a>'

    def block(self, block) -> str:
        if block["type"] == "heading":
            level = min(block["level"], 6)
            return f"<h{level}>{self.inline(block['text'])}</h{level}>"
        if block["type"] == "table":
            return ""
        return super().block(block)

    def figure(self, src: str, alt: str, caption: str, allow_skip: bool = True) -> str:
        entry = self.images.register(src)
        if not entry:
            return f"<p>{self.inline(caption or alt)}</p>"
        caption_html = f"<figcaption>{self.inline(caption)}</figcaption>" if caption else ""
        return (
            f'<figure><img src="{html.escape(entry["url"], quote=True)}" '
            f'alt="{html.escape(alt, quote=True)}">{caption_html}</figure>'
        )

    def gear(self, items) -> str:
        return "\n".join(
            self.figure(item["src"], item["alt"], site.strip_markdown(item["label"]) or item["alt"])
            for item in items
        )


def repo_files_url(plugin_dir: Path):
    url, why = site.git_remote(plugin_dir)
    if not url:
        site.die(f"{plugin_dir.name}: no GitHub address for the images ({why})")
    match = GITHUB_RE.search(url)
    if not match:
        site.die(f"{plugin_dir.name}: origin {url} is not on GitHub")
    return f"https://raw.githubusercontent.com/{match.group(1)}/{match.group(2)}/main"


def read_description(plugin_dir: Path, language: str):
    """The blocks of Description/<language>.md, or None when there is none."""
    path = plugin_dir / "Description" / f"{language}.md"
    if not path.is_file():
        return None
    blocks, _ = site.parse_markdown(path.read_text(encoding="utf-8"))
    return blocks


def store_description(plugin_dir: Path) -> tuple[str, str]:
    """(title, description HTML) for the store page."""
    text = (plugin_dir / "README.md").read_text(encoding="utf-8")
    blocks, refs = site.parse_markdown(text)
    title, intro, sections = site.organize(blocks)
    renderer = StoreRenderer(refs, RepoImages(repo_files_url(plugin_dir)))

    # The README opens with a link to the product page, which the store page
    # does not need. The written description, where there is one, takes the
    # place of the README's opening lines.
    intro = read_description(plugin_dir, "en") or [
        b for b in intro
        if not (b["type"] == "para" and "github.io" in b["text"] and b["text"].startswith("**["))
    ]
    parts = []
    if intro:
        parts.append("<h2>Description</h2>")
        parts.append(renderer.blocks(intro))
    for section in sections:
        if section["title"].strip().lower() in LEFT_OUT:
            continue
        parts.append(f"<h2>{renderer.inline(section['title'])}</h2>")
        parts.append(renderer.blocks(section["blocks"]))
    return title, "\n".join(p for p in parts if p)


def product_data(plugin_dir: Path):
    data = site.load_plugin_data(site.PLUGIN_DATA)
    return site.lookup_extra(data, [site.read_title(plugin_dir), plugin_dir.name])


def first_video_url(entry):
    videos = site.normalize_videos(entry.get("video"))
    return videos[0].get("contentUrl") if videos else None


def page_url(entry):
    website = entry.get("jsonLdIds", {}).get("website", "")
    return website.split("#")[0] or None


def kvr_block(blocks, renderer, indent: str = "") -> str:
    """The description as the indented HTML the KVR form takes."""
    out = []
    for block in blocks:
        if block["type"] == "heading":
            out.append(f"{indent}<h2>{renderer.inline(block['text'])}</h2>")
        elif block["type"] == "para":
            out.append(f"{indent}<p>\n{indent}  {renderer.inline(block['text'])}\n{indent}</p>")
        elif block["type"] == "list":
            out.append(kvr_list(block["items"], renderer, indent))
    return "\n\n".join(out)


def kvr_list(items, renderer, indent: str) -> str:
    lines = [f"{indent}<ul>"]
    for item in items:
        text = renderer.inline(item["text"])
        if item["children"]:
            child = kvr_list(item["children"], renderer, indent + "    ")
            lines.append(f"{indent}  <li>{text}\n{child}\n{indent}  </li>")
        else:
            lines.append(f"{indent}  <li>{text}</li>")
    lines.append(f"{indent}</ul>")
    return "\n".join(lines)


def write_kvr_block(plugin_dir: Path, out_dir: Path):
    blocks = read_description(plugin_dir, "en")
    if blocks is None:
        site.warn(f"{plugin_dir.name}: no Description/en.md, so no KVR text")
        return None
    entry = product_data(plugin_dir)
    renderer = StoreRenderer({}, RepoImages(""))
    title = site.read_title(plugin_dir)
    parts = []
    video = first_video_url(entry)
    if video:
        href = html.escape(video, quote=True)
        parts.append(f'<p>\n  <a href="{href}">{html.escape(video)}</a>\n</p>')
    parts.append(kvr_block(blocks, renderer))
    url = page_url(entry)
    if url:
        parts.append(
            f'<hr />\n<p>\n  <a href="{html.escape(url, quote=True)}">'
            f"Read more about {html.escape(title)} here</a>\n</p>"
        )
    out = out_dir / "kvr.html"
    out.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    return out


def equipment_links(plugin_dir: Path):
    """Equipment the README links to on the site, as (name, site path)."""
    _, refs = site.parse_markdown((plugin_dir / "README.md").read_text(encoding="utf-8"))
    links = []
    for name, url in refs.items():
        if url.startswith(SITE_URL + "equipment/"):
            links.append((name, url[len(SITE_URL):]))
            # A description names the model, the README often its revision too.
            short = re.sub(r"\s+(revision|rev\.?|mk)\s*\w+$", "", name)
            if short != name:
                links.append((short, url[len(SITE_URL):]))
    return links


def site_lines(blocks, links) -> str:
    """The description as the site's plain lines: one line per paragraph, a
    list folded into the line that introduces it, and "Features: ..." for the
    features list. Each piece of equipment is linked where it is first named."""
    def item_text(item):
        text = item["text"]
        if item["children"]:
            text += " " + " ".join(item_text(c) for c in item["children"])
        return text

    lines, heading = [], None
    for block in blocks:
        if block["type"] == "heading":
            heading = block["text"]
        elif block["type"] == "para":
            lines.append(block["text"])
        elif block["type"] == "list":
            items = " ".join(item_text(i) for i in block["items"])
            if heading:
                lines.append(f"{heading}: {items}")
                heading = None
            elif lines and lines[-1].endswith(":"):
                lines[-1] += " " + items
            else:
                lines.append(items)
    text = "\n".join(lines)
    linked = set()
    for name, path in sorted(links, key=lambda l: -len(l[0])):
        if path in linked:
            continue
        pattern = re.compile(r"(?<![\[\w/])" + re.escape(name) + r"(?![\w\]/])", re.I)
        text, count = pattern.subn(lambda m: f"[{m.group(0)}]({path})", text, count=1)
        if count:
            linked.add(path)
    return text


def update_site_file(path: Path, plugin_dirs) -> None:
    raw = path.read_text(encoding="utf-8")
    products = json.loads(raw)
    by_key = {site.match_key(p.get("title", "")): p for p in products}
    changed = []
    for plugin_dir in plugin_dirs:
        entry = by_key.get(site.match_key(site.read_title(plugin_dir))) or by_key.get(
            site.match_key(plugin_dir.name)
        )
        if entry is None:
            site.warn(f"{plugin_dir.name}: no entry with its title in {path.name}")
            continue
        links = equipment_links(plugin_dir)
        content = entry.setdefault("content", {})
        before = dict(content)
        for language in ("en", "no"):
            blocks = read_description(plugin_dir, language)
            if blocks is None:
                site.warn(f"{plugin_dir.name}: no Description/{language}.md, content.{language} left as it is")
                continue
            content[language] = site_lines(blocks, links)
        if content != before:
            changed.append(entry["title"])
    path.write_text(json.dumps(products, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    site.info(f"{site.shown(path)}: " + (", ".join(changed) if changed else "nothing changed"))


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title} for the store</title>
<style>
  body {{ font: 16px/1.5 -apple-system, system-ui, sans-serif; margin: 0; background: #eee; }}
  header {{ position: sticky; top: 0; background: #222; color: #fff; padding: 12px 24px; }}
  header button {{ font: inherit; padding: 6px 14px; margin-right: 12px; }}
  main {{ max-width: 760px; margin: 24px auto; background: #fff; padding: 8px 32px 32px; }}
  img {{ max-width: 100%; }}
  figcaption {{ color: #555; font-size: 14px; }}
</style>
</head>
<body>
<header><button id="copy">Copy description</button><span id="status">Paste it into the store's description editor, below the video.</span></header>
<main id="description">
{body}
</main>
<script>
document.getElementById("copy").addEventListener("click", () => {{
  const range = document.createRange();
  range.selectNodeContents(document.getElementById("description"));
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  const copied = document.execCommand("copy");
  selection.removeAllRanges();
  document.getElementById("status").textContent = copied
    ? "Copied. Paste it into the store's description editor, below the video."
    : "Copying did not work here. Select the description and copy it by hand.";
}});
</script>
</body>
</html>
"""


def write_store_page(plugin_dir: Path, out_dir: Path) -> Path:
    title, body = store_description(plugin_dir)
    out = out_dir / "gumroad.html"
    out.write_text(PAGE.format(title=html.escape(title), body=body), encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Write product descriptions for the other sites.")
    parser.add_argument("plugins", nargs="+", help="plugin directories (e.g. subc-plugin)")
    parser.add_argument(
        "--dehlimusikk", type=Path, metavar="FILE",
        help="the site's product file, whose entries for these plugins get the description",
    )
    args = parser.parse_args(argv)
    plugin_dirs = []
    for name in args.plugins:
        plugin_dir = (ROOT / name).resolve()
        if not (plugin_dir / "README.md").is_file():
            site.die(f"{name}: no README.md")
        plugin_dirs.append(plugin_dir)
        out_dir = OUT_DIR / plugin_dir.name.removesuffix("-plugin")
        out_dir.mkdir(parents=True, exist_ok=True)
        written = [write_store_page(plugin_dir, out_dir), write_kvr_block(plugin_dir, out_dir)]
        site.info("  ".join(site.shown(p) for p in written if p))
    if args.dehlimusikk:
        path = args.dehlimusikk.expanduser().resolve()
        if not path.is_file():
            site.die(f"{args.dehlimusikk}: no such file")
        update_site_file(path, plugin_dirs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
