#!/usr/bin/env python3
"""Add each plugin's sample pack to the Windows installers and Linux tarballs.

The "Windows and Linux builds" workflow builds both on GitHub without samples,
since the packs are paid content and never go there. Download its two
artefacts, then run

    ./dmse bundle <plugin|all> <folder>

where <folder> holds them, as the zip files GitHub gives or unzipped (Safari
unzips them on its own). For every plugin it writes, into build/bundle/:

    <Product>-<version>-windows.zip           Setup.exe, the pack beside it and
                                              Install.txt, in one folder
    <Product>-<version>-linux-x86_64.tar.gz   the tarball with a samples/ folder

and records their SHA-256 checksums in build/bundle/SHA256SUMS.txt. The
version in a file name must be the one in the plugin's CMakeLists.txt, so a
build from before the release commits were pushed is refused.
"""

import argparse
import hashlib
import os
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "bundle"
LINUX_ARCH = "x86_64"

INSTALL_TXT = """{product} {version} for Windows

1. Unzip this whole download.
2. Open the unzipped folder and run {setup}. Keep samples.pak and samples.pak.json beside it, since the installer takes the samples from there.
3. The installer is not code-signed, so Windows may say "Windows protected your PC" the first time. Click More info, then Run anyway.

The VST3 plugin goes to C:\\Program Files\\Common Files\\VST3, the Standalone app to C:\\Program Files\\DehliMusikk\\{product} and the samples to C:\\ProgramData\\DehliMusikk\\{product}. Uninstall it from Settings, Apps.
"""


def plugin_identity(plugin_dir):
    text = (ROOT / plugin_dir / "CMakeLists.txt").read_text(encoding="utf-8")
    product = re.search(r'PRODUCT_NAME\s+"([^"]+)"', text)
    version = re.search(r"\bVERSION\s+([0-9][0-9.]*)", text)
    if not product or not version:
        sys.exit(f"Error: no PRODUCT_NAME or VERSION in {plugin_dir}/CMakeLists.txt")
    return product.group(1), version.group(1)


class Downloads:
    """Every file under the folder, loose or inside a zip, by its own name."""

    def __init__(self, folder):
        self.loose = {}
        self.zipped = {}
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() == ".zip":
                try:
                    with zipfile.ZipFile(path) as z:
                        for member in z.namelist():
                            name = member.rsplit("/", 1)[-1]
                            if name:
                                self.zipped.setdefault(name, (path, member))
                except zipfile.BadZipFile:
                    pass
            else:
                self.loose.setdefault(path.name, path)

    def names(self):
        return set(self.loose) | set(self.zipped)

    def fetch(self, name, work):
        """A path to the named file, unzipped into work if it came in a zip."""
        if name in self.loose:
            return self.loose[name]
        archive, member = self.zipped[name]
        target = work / name
        with zipfile.ZipFile(archive) as z, z.open(member) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        return target


def other_versions(names, prefix, suffix, version):
    """Versions found for this product other than the expected one."""
    found = []
    for name in names:
        if name.startswith(prefix) and name.endswith(suffix):
            middle = name[len(prefix) : len(name) - len(suffix)]
            if re.fullmatch(r"[0-9][0-9.]*", middle) and middle != version:
                found.append(middle)
    return sorted(found)


def write_windows(product, version, setup, pack, out):
    folder = f"{product} {version}"
    install = INSTALL_TXT.format(product=product, version=version, setup=setup.name)
    tmp = out.with_name(out.name + ".part")
    with zipfile.ZipFile(tmp, "w", allowZip64=True) as z:
        z.writestr(f"{folder}/Install.txt", install.replace("\n", "\r\n"), zipfile.ZIP_DEFLATED)
        # The installer and the pack are compressed already.
        z.write(setup, f"{folder}/{setup.name}", zipfile.ZIP_STORED)
        z.write(pack, f"{folder}/samples.pak", zipfile.ZIP_STORED)
        z.write(pack.with_name("samples.pak.json"), f"{folder}/samples.pak.json", zipfile.ZIP_DEFLATED)
    tmp.replace(out)


def write_linux(tarball, pack, out):
    tmp = out.with_name(out.name + ".part")
    with tarfile.open(tarball, "r:gz") as src, tarfile.open(tmp, "w:gz", compresslevel=6) as dst:
        top = None
        for member in src:
            top = top or member.name.split("/", 1)[0]
            if member.name.split("/")[1:2] == ["samples"]:
                continue  # replaced below
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
        if top is None:
            sys.exit(f"Error: {tarball.name} is empty")
        folder = tarfile.TarInfo(f"{top}/samples")
        folder.type = tarfile.DIRTYPE
        folder.mode = 0o755
        dst.addfile(folder)
        for name in ("samples.pak", "samples.pak.json"):
            path = pack.with_name(name)
            info = dst.gettarinfo(str(path), f"{top}/samples/{name}")
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with open(path, "rb") as f:
                dst.addfile(info, f)
    tmp.replace(out)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def update_sums(written):
    sums_file = OUT / "SHA256SUMS.txt"
    sums = {}
    if sums_file.exists():
        for line in sums_file.read_text(encoding="utf-8").splitlines():
            digest, _, name = line.partition("  ")
            if name and (OUT / name).exists():
                sums[name] = digest
    for path in written:
        sums[path.name] = sha256(path)
    sums_file.write_text("".join(f"{sums[n]}  {n}\n" for n in sorted(sums)), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("plugins", nargs="+", help="plugin folders")
    parser.add_argument("--from", dest="source", required=True, help="folder with the downloaded artefacts")
    args = parser.parse_args()

    source = Path(os.environ.get("DMSE_CALLER_DIR", ".")) / Path(args.source).expanduser()
    if not source.is_dir():
        sys.exit(f"Error: {source} is not a folder")
    downloads = Downloads(source)
    names = downloads.names()
    OUT.mkdir(parents=True, exist_ok=True)

    written, problems = [], []
    with tempfile.TemporaryDirectory(prefix="dmse-bundle-") as work_dir:
        work = Path(work_dir)
        for plugin_dir in args.plugins:
            product, version = plugin_identity(plugin_dir)
            safe = product.replace(" ", "-")
            pack = ROOT / plugin_dir / "assets" / "samples" / "samples.pak"
            if not pack.exists() or not pack.with_name("samples.pak.json").exists():
                problems.append(f"{product}: no sample pack in {plugin_dir}/assets/samples, run ./dmse convert first")
                continue

            for system, name, prefix, suffix, out_name in (
                ("Windows", f"{product}-{version}-Setup.exe", f"{product}-", "-Setup.exe",
                 f"{safe}-{version}-windows.zip"),
                ("Linux", f"{safe}-{version}-linux-{LINUX_ARCH}.tar.gz", f"{safe}-",
                 f"-linux-{LINUX_ARCH}.tar.gz", f"{safe}-{version}-linux-{LINUX_ARCH}.tar.gz"),
            ):
                if name not in names:
                    others = other_versions(names, prefix, suffix, version)
                    if others:
                        problems.append(f"{product} for {system}: the download is version {', '.join(others)}, "
                                        f"not {version}. Push the release commits and run the workflow again")
                    else:
                        problems.append(f"{product} for {system}: {name} is not in {source}")
                    continue
                print(f"==> {product} {version} for {system}", flush=True)
                built = downloads.fetch(name, work)
                out = OUT / out_name
                if system == "Windows":
                    write_windows(product, version, built, pack, out)
                else:
                    write_linux(built, pack, out)
                if built.parent == work:
                    built.unlink()
                written.append(out)

    if written:
        update_sums(written)
        print(f"\nWrote {len(written)} file(s) to {OUT.relative_to(ROOT)}/, checksums in SHA256SUMS.txt:")
        for path in written:
            print(f"  {path.name}  ({path.stat().st_size / 1e6:.0f} MB)")
    if problems:
        print("\nNot made:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
