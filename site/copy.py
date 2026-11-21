#!/usr/bin/env python3
"""Write each plugin's product description for the sites that are not built
from the repo, so they are pasted rather than retyped.

    ./dmse copy subc
    ./dmse copy all

For now this writes build/copy/<plugin>/gumroad.html: the README as the store
page shows it, without the title, the link to the product page, the technical
specification (the store's editor has no tables) and the notes about the
repository. Open the page, press "Copy description" and paste it into the
store's description editor below the video, which a paste cannot carry.

Images point at the files in the plugin's public GitHub repository, so they
are the same files the README shows.

Only the Python standard library is used, so it runs on a stock macOS.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_site as site  # noqa: E402

ROOT = site.ROOT
OUT_DIR = ROOT / "build" / "copy"
LEFT_OUT = (site.SPEC_SECTION, site.ABOUT_SECTION)
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


def store_description(plugin_dir: Path) -> tuple[str, str]:
    """(title, description HTML) for the store page."""
    text = (plugin_dir / "README.md").read_text(encoding="utf-8")
    blocks, refs = site.parse_markdown(text)
    title, intro, sections = site.organize(blocks)
    renderer = StoreRenderer(refs, RepoImages(repo_files_url(plugin_dir)))

    # The README opens with a link to the product page, which the store page
    # does not need.
    intro = [
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


def write_store_page(plugin_dir: Path) -> Path:
    title, body = store_description(plugin_dir)
    name = plugin_dir.name.removesuffix("-plugin")
    out = OUT_DIR / name / "gumroad.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(PAGE.format(title=html.escape(title), body=body), encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Write product descriptions for the other sites.")
    parser.add_argument("plugins", nargs="+", help="plugin directories (e.g. subc-plugin)")
    args = parser.parse_args(argv)
    for name in args.plugins:
        plugin_dir = (ROOT / name).resolve()
        if not (plugin_dir / "README.md").is_file():
            site.die(f"{name}: no README.md")
        site.info(f"{site.shown(write_store_page(plugin_dir))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
