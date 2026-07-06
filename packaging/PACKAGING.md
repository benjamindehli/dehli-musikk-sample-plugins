# Packaging the plugins for distribution

Shared, product-neutral tooling for **all** the plugins (Omni-84, MaskinTrommer,
Midnight Wurli, Elektrisk Salmesykkel, the three 4-track instruments). Examples below
use Omni-84; package any other plugin by overriding four env vars (see *Packaging any
plugin*). There are two macOS paths:

- **Path B — Developer ID (paid):** signed + notarized `.pkg`, no Gatekeeper warning.
  Needs the $99/yr Apple Developer Program. **This is the current path** — all seven
  macOS installers are built this way.
- **Path A — Free (ad-hoc):** no Apple Developer account. Ships an unsigned `.pkg`;
  buyers do a one-time right-click ▸ Open. Fallback only.
- **Windows:** an Inno Setup installer for the VST3 + Standalone (Authenticode
  signing optional, also free-without-cert).

> Build host: macOS packaging happens on your Mac; Windows on a Windows machine.
> The Linux dev sandbox can't sign.

## Packaging any plugin — or all of them

Per-plugin identity (product name, bundle id, plugin dir, version, Windows installer
GUID) is emitted by CMake at configure time into `build/dmse_plugins/<Target>.json`
(from each plugin's `dmse_add_plugin(...)` call — the single source of truth). The
drivers read those, so **nothing identity-related is typed by hand anymore**:

```bash
# macOS — everything (build + sign + notarize each product):
DEV_ID_APP="Developer ID Application: NAME (TEAMID)" \
DEV_ID_INSTALLER="Developer ID Installer: NAME (TEAMID)" \
NOTARY_PROFILE="omni84-notary" \
  packaging/macos/package_all.sh

# ...or a subset by target name:
...  packaging/macos/package_all.sh StyloPoly SubC

# Windows — everything (build + Inno installer per product):
powershell -ExecutionPolicy Bypass -File packaging\windows\make_installers.ps1
```

Each product's `.pkg` lands in its own `<plugin>/packaging/macos/build/<Target>/`;
Windows setups land in `packaging/windows/build/`.

**Sample packs ship inside the installers**: both packagers detect
`<plugin>/assets/samples/samples.pak` and install it system-wide
(macOS `/Library/Application Support/DehliMusikk/<product>/`, Windows
`C:\ProgramData\DehliMusikk\<product>\`) — the engine falls back there when the
per-user dev path is absent. Reconvert BEFORE packaging so the pack is present;
installers for packed plugins are correspondingly large (the samples live there). (The `omni84-notary` profile
authenticates your Apple account, so it works for every plugin.)

To package ONE plugin by hand, pass the five identity vars explicitly (they are
required — there are no defaults): values come from `build/dmse_plugins/<Target>.json`.

```bash
cmake --build build --target <TARGET>_All
DEV_ID_APP="Developer ID Application: NAME (TEAMID)" \
DEV_ID_INSTALLER="Developer ID Installer: NAME (TEAMID)" \
NOTARY_PROFILE="omni84-notary" \
PRODUCT="…" BUNDLE_ID="…" PLUGIN_DIR="…" TARGET="…" VERSION="…" \
  packaging/macos/sign_and_package.sh
```

---

## 1. macOS — Path A: Free (ad-hoc)  ← current

No account, no certs. Ad-hoc signing just makes the binaries runnable on Apple
Silicon; the installer is unsigned, so buyers get a one-time Gatekeeper prompt.

```bash
# from the workspace root
cmake -B build -G "Unix Makefiles" -DCMAKE_BUILD_TYPE=Release
cmake --build build --target Omni84_All --config Release

ADHOC=1 packaging/macos/sign_and_package.sh
```

Result: `omni-84-plugin/packaging/macos/build/Omni-84-<version>.pkg` (unsigned).

### Opening an un-notarized build — note to ship buyers

> **macOS:** because Omni-84 isn't notarized yet, macOS will say it's "from an
> unidentified developer" the first time. To install:
>
> 1. **Right-click (or Control-click) the `.pkg` ▸ Open ▸ Open.** You only do this once.
> 2. **On macOS 15 (Sequoia) or later**, if there's no "Open" button on that prompt:
>    just click **OK / Done**, then go to **System Settings ▸ Privacy & Security**,
>    scroll to the bottom, and click **"Open Anyway"** next to the Omni-84 message
>    (you may need your password / Touch ID). Then re-open the `.pkg`.
>
> The plugin then appears in your DAW as usual. (If a plugin is still blocked, run in
> Terminal: `xattr -dr com.apple.quarantine "/Library/Audio/Plug-Ins/VST3/Omni-84.vst3"`.)

---

## 2. macOS — Path B: Developer ID (paid, for later)

### 2a. One-time prerequisites

1. **Apple Developer Program** membership ($99/yr).
2. Two **Developer ID** certificates (Xcode ▸ Settings ▸ Accounts ▸ *Manage
   Certificates* ▸ **+**, or the Apple Developer portal):
   - **Developer ID Application** — signs the `.vst3` / `.component` / `.app`.
   - **Developer ID Installer** — signs the `.pkg`.

   Confirm they're installed (with their private keys, on this Mac):
   ```bash
   security find-identity -v -p codesigning | grep "Developer ID"
   ```
   Note the full string incl. the team id, e.g.
   `Developer ID Application: Your Name (AB12CD34EF)`.

3. **Notarization credentials** — pick ONE method and store it as a notarytool
   *keychain profile* named `omni84-notary`:

   **Method A — App Store Connect API key (recommended).** App Store Connect ▸
   Users and Access ▸ Integrations ▸ App Store Connect API → create a key, download
   `AuthKey_XXXXXX.p8`, note the **Key ID** and **Issuer ID**:
   ```bash
   xcrun notarytool store-credentials "omni84-notary" \
     --key /secure/path/AuthKey_XXXXXX.p8 --key-id <KEY_ID> --issuer <ISSUER_ID>
   ```

   **Method B — Apple ID + app-specific password.** Create one at
   <https://appleid.apple.com> (Sign-In and Security ▸ App-Specific Passwords):
   ```bash
   xcrun notarytool store-credentials "omni84-notary" \
     --apple-id you@example.com --team-id <TEAMID> --password <app-specific-password>
   ```

   The secret lives in the login keychain — **never commit the `.p8`, `.p12`, or
   passwords** (`.gitignore` already blocks them).

### 2b. Build + package + notarize

```bash
cmake --build build --target Omni84_All --config Release

DEV_ID_APP="Developer ID Application: Your Name (AB12CD34EF)" \
DEV_ID_INSTALLER="Developer ID Installer: Your Name (AB12CD34EF)" \
NOTARY_PROFILE="omni84-notary" \
  packaging/macos/sign_and_package.sh
```

Signed, notarized, stapled. Verify on a clean Mac:
```bash
spctl --assess --type install --verbose=2 Omni-84-<version>.pkg   # "accepted"
```

---

## 3. Windows

### 3a. Prerequisites
- Build toolchain: Visual Studio (MSVC) + CMake.
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer (`ISCC.exe`).
- *(Optional)* an Authenticode code-signing certificate (OV/EV) + `signtool`.

### 3b. Build + package
```bat
cmake -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --target Omni84_All --config Release
ISCC /DMyVersion=0.1.0 packaging\windows\installer.iss
```
Output: `omni-84-plugin\packaging\windows\build\Omni-84-0.1.0-Setup.exe`.
Installs the VST3 to `C:\Program Files\Common Files\VST3` and the Standalone to
`C:\Program Files\DehliMusikk\Omni-84`.

### 3c. (Optional) Authenticode signing
Sign the Standalone `.exe` *before* compiling the installer, then the installer:
```bat
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
  /a "build\omni-84-plugin\Omni84_artefacts\Release\Standalone\Omni-84.exe"
:: ...build the installer, then...
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
  /a "omni-84-plugin\packaging\windows\build\Omni-84-0.1.0-Setup.exe"
```
Without a cert, Windows SmartScreen will warn until the download earns reputation.

---

## 4. Versioning

Bump the version in **one** place — the plugin's `CMakeLists.txt`:
```cmake
dmse_add_plugin(StyloPoly
    PRODUCT_NAME "StyloPoly"
    PLUGIN_CODE  Styl
    VERSION      1.0.0    # <- bump here
)
```
Reconfigure (`cmake -B build`) and the packagers pick it up automatically via
`build/dmse_plugins/<Target>.json` — the binary, artifact names and installer
metadata can no longer disagree.

---

## 5. Before you ship

- **Trademark:** "Omnichord" and "Suzuki" are Suzuki's marks. Settle the
  product naming/branding with legal before public sale (PLAN.md risk #4).
- **Paid samples stay private:** the installer embeds the generated `assets/`
  bundle, which is *not* in the public repo — regenerate it with `dmse_convert`
  from your private library before building for release.
