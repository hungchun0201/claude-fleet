#!/usr/bin/env bash
# Install the Claude Fleet Focus companion extension so the dashboard's "Focus"
# button can raise a specific VS Code integrated terminal.
#
# Installs the unpacked extension into ~/.vscode/extensions (the dir shared by
# VS Code-family editors whose dataFolderName is ".vscode" — VS Code, VSCodium,
# and renamed builds). For Cursor, pass its extensions dir as $1.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="$PWD/vscode-extension"
VER="$(python3 -c "import json;print(json.load(open('$SRC/package.json'))['version'])")"
EXT_DIR="${1:-$HOME/.vscode/extensions}"
DEST="$EXT_DIR/claude-fleet.claude-fleet-focus-$VER"

mkdir -p "$DEST"
cp "$SRC/package.json" "$SRC/extension.js" "$SRC/README.md" "$DEST/"

echo "✓ Installed Claude Fleet Focus → $DEST"
echo
echo "Now reload the editor window to activate it:"
echo "    ⇧⌘P → Developer: Reload Window"
echo "(Integrated terminals persist across the reload.)"
