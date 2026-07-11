#!/usr/bin/env bash
#
# @PRODUCT@ @VERSION@ — Linux installer (per-user, no root needed).
#
#   ./install.sh              install VST3 + Standalone + samples for this user
#   ./install.sh --uninstall  remove them again
#
# Installs to standard per-user locations:
#   VST3        ~/.vst3/@PRODUCT@.vst3            (scanned by REAPER, Bitwig, Ardour, ...)
#   Standalone  ~/.local/bin/@PRODUCT@ (+ .desktop entry)
#   Samples     ~/.local/share/DehliMusikk/@PRODUCT@/samples.pak

set -euo pipefail
cd "$(dirname "$0")"

PRODUCT="@PRODUCT@"
VST3_DEST="$HOME/.vst3"
BIN_DEST="$HOME/.local/bin"
DATA_DEST="$HOME/.local/share/DehliMusikk/$PRODUCT"
DESKTOP_DEST="$HOME/.local/share/applications"

if [ "${1:-}" = "--uninstall" ]; then
    echo "Removing $PRODUCT ..."
    rm -rf "$VST3_DEST/$PRODUCT.vst3" "$DATA_DEST"
    rm -f  "$BIN_DEST/$PRODUCT" "$DESKTOP_DEST/dehlimusikk-$PRODUCT.desktop"
    echo "Done. (Per-user settings in ~/.config/DehliMusikk are kept.)"
    exit 0
fi

echo "Installing $PRODUCT ..."

if [ -d "vst3/$PRODUCT.vst3" ]; then
    mkdir -p "$VST3_DEST"
    rm -rf "$VST3_DEST/$PRODUCT.vst3"
    cp -r "vst3/$PRODUCT.vst3" "$VST3_DEST/"
    echo "  VST3       -> $VST3_DEST/$PRODUCT.vst3"
fi

if [ -f "standalone/$PRODUCT" ]; then
    mkdir -p "$BIN_DEST" "$DESKTOP_DEST"
    cp "standalone/$PRODUCT" "$BIN_DEST/"
    chmod +x "$BIN_DEST/$PRODUCT"

    # App icon: lives with the product's data; the .desktop entry uses the
    # absolute path (works everywhere, no icon-theme cache to refresh).
    ICON_LINE=""
    if [ -f "icon.png" ]; then
        mkdir -p "$DATA_DEST"
        cp icon.png "$DATA_DEST/icon.png"
        ICON_LINE="Icon=$DATA_DEST/icon.png"
    fi

    cat > "$DESKTOP_DEST/dehlimusikk-$PRODUCT.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$PRODUCT
Comment=Dehli Musikk sample instrument
Exec="$BIN_DEST/$PRODUCT"
Categories=AudioVideo;Audio;
Terminal=false
$ICON_LINE
EOF
    echo "  Standalone -> $BIN_DEST/$PRODUCT"
fi

if [ -f "samples/samples.pak" ]; then
    mkdir -p "$DATA_DEST"
    cp samples/samples.pak samples/samples.pak.json "$DATA_DEST/"
    echo "  Samples    -> $DATA_DEST/"
fi

echo ""
echo "✅ $PRODUCT installed. Rescan plugins in your DAW (VST3 path: ~/.vst3)."
echo "   Uninstall any time with:  ./install.sh --uninstall"
