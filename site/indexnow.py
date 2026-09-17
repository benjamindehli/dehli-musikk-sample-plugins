#!/usr/bin/env python3
"""Tell the IndexNow search engines which product pages have changed.

IndexNow is a ping: you host a key file at the root of the domain, then POST a
list of changed URLs and the participating engines (Bing, Yandex, Seznam, Naver,
Yep) come and look. Google is not one of them, so this supplements the sitemap
index rather than replacing it.

Nothing here needs a server. The key file is served by the user site, which owns
the root of benjamindehli.github.io, and a key at the root authorises every URL
on that host, including all thirteen project sites underneath it.

The URLs come from the sitemaps ./dmse site already wrote, so this never invents
one, and the <lastmod> in those sitemaps is what decides whether a page counts as
changed. Submitting unchanged URLs is the thing IndexNow asks you not to do, so
what was sent last time is remembered in site/.indexnow-state.json and only the
difference goes out.

    ./dmse indexnow all             # show what would be sent, send nothing
    ./dmse indexnow all --submit    # actually send it
    ./dmse indexnow all --all --submit

Only the standard library is used, so it runs on a stock macOS.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent
ROOT = SITE_DIR.parent

# The shared endpoint forwards a submission to every participating engine, so
# there is no reason to ping bing.com and yandex.com separately.
DEFAULT_ENDPOINT = "https://api.indexnow.org/indexnow"
KEY_FILE = SITE_DIR / "indexnow-key.txt"
STATE_FILE = SITE_DIR / ".indexnow-state.json"
SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
# IndexNow accepts 10000 URLs per request. Thirteen plugins come nowhere near
# it, but a batch that silently lost its tail would be worse than a slow loop.
MAX_URLS = 10000
KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")


def info(msg: str) -> None:
    print(f"\033[1m==>\033[0m {msg}" if sys.stdout.isatty() else f"==> {msg}")


def warn(msg: str) -> None:
    print(f"\033[33m!!\033[0m {msg}" if sys.stderr.isatty() else f"!! {msg}", file=sys.stderr)


def die(msg: str) -> "NoReturn":  # noqa: F821
    print(f"Error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def read_key(explicit=None) -> str:
    """The IndexNow key, from --key, the environment, or site/indexnow-key.txt.

    The key is not a secret. It is published at the root of the domain as
    <key>.txt, because serving it there is the whole proof of ownership, so
    committing it alongside the generator is fine and saves a setup step.
    """
    import os

    key = (explicit or os.environ.get("DMSE_INDEXNOW_KEY") or "").strip()
    source = "--key" if explicit else "DMSE_INDEXNOW_KEY"
    if not key and KEY_FILE.is_file():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        source = KEY_FILE.name
    if not key:
        die(
            "no IndexNow key.\n"
            f"  Put the key in {KEY_FILE.relative_to(ROOT)}, set DMSE_INDEXNOW_KEY,\n"
            "  or pass --key. It is the same key as the <key>.txt published at\n"
            "  the root of the user site, and it is public by design."
        )
    if not KEY_RE.fullmatch(key):
        die(f"the key from {source} is not 8 to 128 characters of letters, digits and dashes: {key!r}")
    return key


def sitemap_entries(path: Path):
    """[(url, lastmod or None)] from one sitemap.xml."""
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        warn(f"{path.relative_to(ROOT)}: {exc}")
        return []
    entries = []
    for node in tree.getroot().findall(f"{SITEMAP_NS}url"):
        loc = node.findtext(f"{SITEMAP_NS}loc")
        if not loc:
            continue
        entries.append((loc.strip(), (node.findtext(f"{SITEMAP_NS}lastmod") or "").strip() or None))
    return entries


def collect(plugin_dirs, out: str):
    """Every URL the named plugins published, with the lastmod beside it."""
    found, missing = {}, []
    for plugin_dir in plugin_dirs:
        sitemap = plugin_dir / out / "sitemap.xml"
        if not sitemap.is_file():
            missing.append(plugin_dir.name)
            continue
        for url, lastmod in sitemap_entries(sitemap):
            found[url] = lastmod
    if missing:
        warn(
            f"no {out}/sitemap.xml for {', '.join(missing)} — run ./dmse site first, "
            "since only a generated page can be announced"
        )
    return found


def load_state():
    if not STATE_FILE.is_file():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("submitted", {})
    except (json.JSONDecodeError, OSError) as exc:
        warn(f"{STATE_FILE.name} unreadable ({exc}), treating every URL as new")
        return {}


def save_state(submitted) -> None:
    STATE_FILE.write_text(
        json.dumps({"submitted": submitted}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def changed_since(found, submitted, force: bool):
    """URLs to announce: new ones, and ones whose lastmod moved."""
    if force:
        return sorted(found)
    return sorted(url for url, lastmod in found.items() if submitted.get(url) != lastmod)


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc


def submit(endpoint: str, host: str, key: str, key_location: str, urls):
    """POST one batch. Returns (status, body) or raises for a transport failure."""
    payload = json.dumps(
        {"host": host, "key": key, "keyLocation": key_location, "urlList": list(urls)}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "dmse-indexnow/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace").strip()


# What the protocol's status codes actually mean, since "403" on its own sends
# you looking in the wrong place.
STATUS_HELP = {
    200: "accepted",
    202: "accepted, the key is still being validated",
    400: "bad request, the payload was malformed",
    403: "the key was rejected: check <key>.txt is reachable at the URL below",
    422: "the URLs do not match the host, or the key does not match the key file",
    429: "too many requests, try again later",
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Announce changed product pages to the IndexNow search engines."
    )
    parser.add_argument("plugins", nargs="*", help="plugin directories (default: all)")
    parser.add_argument("--submit", action="store_true",
                        help="actually send it (the default is to show what would be sent)")
    parser.add_argument("--all", action="store_true", dest="force",
                        help="send every URL, not only the ones whose lastmod moved")
    parser.add_argument("--url", action="append", default=[],
                        help="an extra URL to include, e.g. the user site's own page")
    parser.add_argument("--key", help="the IndexNow key (default: DMSE_INDEXNOW_KEY or site/indexnow-key.txt)")
    parser.add_argument("--key-location", help="where the key file is served (default: the host root)")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help=f"default: {DEFAULT_ENDPOINT}")
    parser.add_argument("--out", default="docs", help="the generated folder inside each plugin repo")
    args = parser.parse_args(argv)

    names = [n for n in args.plugins if n != "all"] if args.plugins != ["all"] else []
    if names:
        plugin_dirs = []
        for name in names:
            path = (ROOT / name).resolve()
            if not path.is_dir():
                die(f"no such plugin directory: {name}")
            plugin_dirs.append(path)
    else:
        plugin_dirs = sorted(ROOT.glob("*-plugin"))

    found = collect(plugin_dirs, args.out)
    for url in args.url:
        found.setdefault(url, None)  # no lastmod, so it goes every time it is asked for
    if not found:
        die("no URLs found. Run ./dmse site first.")

    hosts = {host_of(url) for url in found}
    if len(hosts) != 1:
        die(f"the URLs span more than one host, and one key covers one host: {sorted(hosts)}")
    host = hosts.pop()

    key = read_key(args.key)
    key_location = args.key_location or f"https://{host}/{key}.txt"

    submitted = load_state()
    urls = changed_since(found, submitted, args.force)
    unchanged = len(found) - len(urls)

    if not urls:
        info(f"nothing to announce: all {len(found)} URLs are unchanged since the last run")
        info("Use --all to send them anyway.")
        return 0

    info(f"{len(urls)} changed URL{'s' if len(urls) != 1 else ''} on {host}"
         + (f", {unchanged} unchanged and skipped" if unchanged else ""))
    for url in urls:
        print(f"    {url}")
    print(f"  key file: {key_location}")

    if not args.submit:
        info("Dry run. Nothing was sent. Add --submit to send it.")
        return 0

    sent = 0
    for start in range(0, len(urls), MAX_URLS):
        batch = urls[start:start + MAX_URLS]
        status, body = submit(args.endpoint, host, key, key_location, batch)
        meaning = STATUS_HELP.get(status, "unexpected status")
        if status in (200, 202):
            info(f"{status} {meaning} ({len(batch)} URLs)")
            sent += len(batch)
        else:
            warn(f"{status} {meaning}{': ' + body if body else ''}")
            warn(f"  check {key_location} serves exactly the key and nothing else")
            return 1

    # Recorded only for what the engines accepted, so a failed run retries.
    for url in urls[:sent]:
        submitted[url] = found[url]
    save_state(submitted)
    info(f"Recorded {sent} URLs in {STATE_FILE.name}; the next run will skip them until they change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
