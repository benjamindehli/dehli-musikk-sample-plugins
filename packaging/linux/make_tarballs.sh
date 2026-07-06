#!/usr/bin/env bash
#
# Build Linux release tarballs for ALL plugins — or a subset by target name.
#
# Like packaging/macos/package_all.sh, identity comes from the metadata CMake emits
# (build dir/dmse_plugins/<Target>.json). Run on a Linux machine (or the dev container)
# AFTER configuring a Linux build dir:
#
#   cmake -B build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release
#   packaging/linux/make_tarballs.sh
#   packaging/linux/make_tarballs.sh StyloPoly SubC        # subset
#
# Each tarball contains vst3/ + standalone/ + samples/ (the memory-mapped pack) and a
# per-user install.sh (no root needed). Output: <plugin>/packaging/linux/build/
#   <Product>-<version>-linux-<arch>.tar.gz

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$REPO_ROOT/build-linux}"
META_DIR="$BUILD_DIR/dmse_plugins"
ARCH="$(uname -m)"

if [ ! -d "$META_DIR" ] || ! ls "$META_DIR"/*.json >/dev/null 2>&1; then
    echo "ERROR: no plugin metadata in $META_DIR — configure first:"
    echo "  cmake -B \"$BUILD_DIR\" -G Ninja -DCMAKE_BUILD_TYPE=Release"
    exit 1
fi

only=("$@")
packaged=()
for meta in "$META_DIR"/*.json; do
    eval "$(python3 - "$meta" <<'PY'
import json, sys, shlex
m = json.load(open(sys.argv[1]))
for k, v in [("PRODUCT", m["product"]), ("PLUGIN_DIR", m["dir"]),
             ("TARGET", m["target"]), ("VERSION", m["version"])]:
    print(f"export {k}={shlex.quote(str(v))}")
PY
)"
    if [ "${#only[@]}" -gt 0 ]; then
        case " ${only[*]} " in
            *" $TARGET "*) ;;
            *) continue ;;
        esac
    fi

    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "  $TARGET — $PRODUCT $VERSION (linux-$ARCH)"
    echo "════════════════════════════════════════════════════════════════"
    cmake --build "$BUILD_DIR" --target "${TARGET}_All"

    ART="$BUILD_DIR/$PLUGIN_DIR/${TARGET}_artefacts/Release"
    [ -d "$ART" ] || ART="$BUILD_DIR/$PLUGIN_DIR/${TARGET}_artefacts"   # single-config generators
    VST3="$ART/VST3/$PRODUCT.vst3"
    APP="$ART/Standalone/$PRODUCT"
    [ -d "$VST3" ] || { echo "ERROR: missing $VST3"; exit 1; }
    [ -f "$APP" ]  || { echo "ERROR: missing $APP"; exit 1; }

    OUT="$REPO_ROOT/$PLUGIN_DIR/packaging/linux/build"
    STAGE="$OUT/stage/$PRODUCT-$VERSION"
    rm -rf "$OUT/stage"
    mkdir -p "$STAGE/vst3" "$STAGE/standalone"
    cp -r "$VST3" "$STAGE/vst3/"
    cp "$APP" "$STAGE/standalone/"

    # Memory-mapped sample pack — without it the plugin is silent (embedded-sample
    # plugins have no pack and skip this).
    PACK="$REPO_ROOT/$PLUGIN_DIR/assets/samples/samples.pak"
    if [ -f "$PACK" ]; then
        mkdir -p "$STAGE/samples"
        cp "$PACK" "$PACK.json" "$STAGE/samples/"
    fi

    sed "s/@PRODUCT@/$PRODUCT/g; s/@VERSION@/$VERSION/g" \
        "$SCRIPT_DIR/install_template.sh" > "$STAGE/install.sh"
    chmod +x "$STAGE/install.sh"

    # Hyphenate spaces in the DOWNLOAD FILE name (friendly for links/curl); the
    # directory inside the tarball keeps the real product name for install.sh.
    SAFE_NAME="${PRODUCT// /-}"
    TARBALL="$OUT/$SAFE_NAME-$VERSION-linux-$ARCH.tar.gz"
    tar -C "$OUT/stage" -czf "$TARBALL" "$PRODUCT-$VERSION"
    rm -rf "$OUT/stage"
    echo "✅ $TARBALL ($(du -h "$TARBALL" | cut -f1 | tr -d ' '))"
    packaged+=("$PRODUCT-$VERSION")
done

echo ""
if [ "${#packaged[@]}" -eq 0 ]; then
    echo "Nothing matched (${only[*]:-}). Known targets:"
    ls "$META_DIR" | sed 's/\.json$//' | sed 's/^/  /'
    exit 1
fi
echo "✅ Packaged ${#packaged[@]} product(s): ${packaged[*]}"
