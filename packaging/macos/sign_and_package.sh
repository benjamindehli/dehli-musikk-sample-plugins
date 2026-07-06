#!/usr/bin/env bash
#
# macOS sign + package (+ notarize) — SHARED across all plugins.
#
# Product-neutral: the five identity vars are REQUIRED (no defaults — a forgotten one
# used to silently package/mislabel Omni-84). Prefer package_all.sh, which derives all
# of them from the per-plugin metadata CMake emits (build/dmse_plugins/<Target>.json).
# The installer lands in the plugin's own folder.
#
# Two modes:
#   FREE (ad-hoc) — no Apple Developer account. Ad-hoc signs the artifacts (so they
#     run on Apple Silicon) and builds an UNSIGNED, un-notarized .pkg. Buyers get a
#     one-time Gatekeeper "unidentified developer" prompt (see ../PACKAGING.md).
#         cmake --build build --target <TARGET>_All --config Release
#         ADHOC=1 PRODUCT=... BUNDLE_ID=... PLUGIN_DIR=... TARGET=... VERSION=... \
#           packaging/macos/sign_and_package.sh
#
#   PAID (Developer ID) — proper signed + notarized installer, no warnings.
#         DEV_ID_APP="Developer ID Application: NAME (TEAMID)" \
#         DEV_ID_INSTALLER="Developer ID Installer: NAME (TEAMID)" \
#         NOTARY_PROFILE="omni84-notary" \
#         PRODUCT="MaskinTrommer" BUNDLE_ID="com.dehlimusikk.maskintrommer" \
#         PLUGIN_DIR="maskintrommer-plugin" TARGET="MaskinTrommer" VERSION="0.1.0" \
#           packaging/macos/sign_and_package.sh
#
# Run AFTER a Release build of all formats (<TARGET>_All). See ../PACKAGING.md.

set -euo pipefail

# ---- config (via environment) ---------------------------------------------
ADHOC="${ADHOC:-0}"                    # 1 = free ad-hoc mode
DEV_ID_APP="${DEV_ID_APP:-Developer ID Application: CHANGE ME (TEAMID)}"
DEV_ID_INSTALLER="${DEV_ID_INSTALLER:-Developer ID Installer: CHANGE ME (TEAMID)}"
NOTARY_PROFILE="${NOTARY_PROFILE:-}"   # notarytool keychain profile; empty = skip notarization
# Per-plugin identity — REQUIRED. PLUGIN_DIR = repo subfolder, TARGET = juce target
# (its <TARGET>_artefacts folder), PRODUCT = PRODUCT_NAME (the .app/.vst3 base name).
: "${PRODUCT:?Set PRODUCT (e.g. \"StyloPoly\") — or use package_all.sh}"
: "${BUNDLE_ID:?Set BUNDLE_ID (e.g. com.dehlimusikk.stylopoly) — or use package_all.sh}"
: "${PLUGIN_DIR:?Set PLUGIN_DIR (e.g. stylopoly-plugin) — or use package_all.sh}"
: "${TARGET:?Set TARGET (e.g. StyloPoly) — or use package_all.sh}"
: "${VERSION:?Set VERSION (e.g. 1.0.0) — or use package_all.sh}"
# --------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"   # packaging/macos -> repo root
BUILD_DIR="${BUILD_DIR:-$REPO_ROOT/build}"
ART="$BUILD_DIR/$PLUGIN_DIR/${TARGET}_artefacts/Release"
# Output lands in the plugin being packaged (not the shared script's folder), so each
# product's installer sits with its own repo. Omni-84 resolves to the same path as before.
OUT="$REPO_ROOT/$PLUGIN_DIR/packaging/macos/build/$TARGET"
ENTITLEMENTS="$SCRIPT_DIR/Standalone.entitlements"

VST3="$ART/VST3/$PRODUCT.vst3"
AU="$ART/AU/$PRODUCT.component"
APP="$ART/Standalone/$PRODUCT.app"

for p in "$VST3" "$AU" "$APP"; do
    [ -e "$p" ] || { echo "ERROR: missing artifact: $p
Build first:  cmake --build \"$BUILD_DIR\" --target ${TARGET}_All --config Release"; exit 1; }
done

if [ "$ADHOC" = "1" ]; then
    echo "==> Mode: FREE (ad-hoc) — unsigned, un-notarized installer"
    echo "==> Ad-hoc codesigning artifacts"
    codesign --force -s - "$VST3"
    codesign --force -s - "$AU"
    codesign --force -s - "$APP"
else
    echo "==> Mode: Developer ID — signed + notarized"
    echo "==> Codesigning (hardened runtime + secure timestamp) with: $DEV_ID_APP"
    codesign --force --options runtime --timestamp --sign "$DEV_ID_APP" "$VST3"
    codesign --force --options runtime --timestamp --sign "$DEV_ID_APP" "$AU"
    codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" --sign "$DEV_ID_APP" "$APP"
fi

echo "==> Verifying signatures"
for p in "$VST3" "$AU" "$APP"; do codesign --verify --strict --verbose=2 "$p"; done

echo "==> Staging install layout"
STAGE="$OUT/stage"
rm -rf "$OUT"
mkdir -p "$STAGE/Library/Audio/Plug-Ins/VST3" \
         "$STAGE/Library/Audio/Plug-Ins/Components" \
         "$STAGE/Applications"
cp -R "$VST3" "$STAGE/Library/Audio/Plug-Ins/VST3/"
cp -R "$AU"   "$STAGE/Library/Audio/Plug-Ins/Components/"
cp -R "$APP"  "$STAGE/Applications/"

echo "==> Building component package (relocation disabled so paths are fixed)"
COMPONENT_PKG="$OUT/$PRODUCT-component.pkg"
COMPONENTS_PLIST="$OUT/components.plist"
pkgbuild --analyze --root "$STAGE" "$COMPONENTS_PLIST"
i=0
while /usr/libexec/PlistBuddy -c "Print :$i:BundleIsRelocatable" "$COMPONENTS_PLIST" >/dev/null 2>&1; do
    /usr/libexec/PlistBuddy -c "Set :$i:BundleIsRelocatable false" "$COMPONENTS_PLIST"
    i=$((i + 1))
done
pkgbuild --root "$STAGE" --install-location "/" --component-plist "$COMPONENTS_PLIST" \
         --identifier "$BUNDLE_ID.pkg" --version "$VERSION" "$COMPONENT_PKG"

PRODUCT_PKG="$OUT/$PRODUCT-$VERSION.pkg"
productbuild --synthesize --package "$COMPONENT_PKG" "$OUT/distribution.xml"

if [ "$ADHOC" = "1" ]; then
    echo "==> Building UNSIGNED product installer"
    productbuild --distribution "$OUT/distribution.xml" --package-path "$OUT" "$PRODUCT_PKG"
    echo ""
    echo "✅ Free ad-hoc installer: $PRODUCT_PKG"
    echo "   Not notarized — buyers open it via right-click ▸ Open the first time."
    echo "   (See ../PACKAGING.md ▸ 'Opening an un-notarized build' for the note to ship them.)"
    exit 0
fi

echo "==> Building signed product installer with: $DEV_ID_INSTALLER"
productbuild --distribution "$OUT/distribution.xml" --package-path "$OUT" \
             --sign "$DEV_ID_INSTALLER" "$PRODUCT_PKG"

if [ -n "$NOTARY_PROFILE" ]; then
    echo "==> Notarizing (waits for Apple; can take a few minutes)"
    xcrun notarytool submit "$PRODUCT_PKG" --keychain-profile "$NOTARY_PROFILE" --wait
    echo "==> Stapling + validating"
    xcrun stapler staple "$PRODUCT_PKG"
    xcrun stapler validate "$PRODUCT_PKG"
    spctl --assess --type install --verbose=2 "$PRODUCT_PKG" || true
    echo "✅ Notarized installer: $PRODUCT_PKG"
else
    echo "⚠️  NOTARY_PROFILE not set — signed but NOT notarized: $PRODUCT_PKG"
    echo "    Buyers would see a Gatekeeper warning. See ../PACKAGING.md to set up notarization."
fi
