#!/usr/bin/env bash
#
# Build + sign (+ notarize) installers for ALL plugins — or a subset by target name.
#
# Identity (product name, bundle id, plugin dir, version) comes from the metadata
# dmse_add_plugin emits at CMake configure time (build/dmse_plugins/<Target>.json),
# so there is nothing to keep in sync by hand. Run `cmake -B build` first.
#
# Usage:
#   # everything, signed + notarized:
#   DEV_ID_APP="Developer ID Application: Benjamin Dehli (97P6P6SY2J)" \
#   DEV_ID_INSTALLER="Developer ID Installer: Benjamin Dehli (97P6P6SY2J)" \
#   NOTARY_PROFILE="omni84-notary" \
#     packaging/macos/package_all.sh
#
#   # a subset:
#   ... packaging/macos/package_all.sh StyloPoly SubC
#
#   # free ad-hoc mode:
#   ADHOC=1 packaging/macos/package_all.sh
#
# Each product's .pkg lands in <plugin>/packaging/macos/build/<Target>/ as before.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="${BUILD_DIR:-$REPO_ROOT/build}"
META_DIR="$BUILD_DIR/dmse_plugins"

if [ ! -d "$META_DIR" ] || ! ls "$META_DIR"/*.json >/dev/null 2>&1; then
    echo "ERROR: no plugin metadata in $META_DIR — configure first:  cmake -B \"$BUILD_DIR\""
    exit 1
fi

only=("$@")
packaged=()
for meta in "$META_DIR"/*.json; do
    # Export PRODUCT/BUNDLE_ID/PLUGIN_DIR/TARGET/VERSION (shell-quoted) from the json.
    eval "$(python3 - "$meta" <<'PY'
import json, sys, shlex
m = json.load(open(sys.argv[1]))
for k, v in [("PRODUCT", m["product"]), ("BUNDLE_ID", m["bundleId"]),
             ("PLUGIN_DIR", m["dir"]), ("TARGET", m["target"]), ("VERSION", m["version"])]:
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
    echo "  $TARGET — $PRODUCT $VERSION ($BUNDLE_ID)"
    echo "════════════════════════════════════════════════════════════════"
    cmake --build "$BUILD_DIR" --target "${TARGET}_All" --config Release
    "$SCRIPT_DIR/sign_and_package.sh"
    packaged+=("$PRODUCT-$VERSION")
done

echo ""
if [ "${#packaged[@]}" -eq 0 ]; then
    echo "Nothing matched (${only[*]:-}). Known targets:"
    ls "$META_DIR" | sed 's/\.json$//' | sed 's/^/  /'
    exit 1
fi
echo "✅ Packaged ${#packaged[@]} product(s): ${packaged[*]}"
