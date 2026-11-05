#!/bin/sh
set -e
ROOT=$(cd "$(dirname "$0")" && pwd)
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"
chmod +x "$ROOT/bin/approve"
ln -sf "$ROOT/bin/approve" "$BIN_DIR/approve"
echo "Installé : $BIN_DIR/approve"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Attention : $BIN_DIR n'est pas dans ton PATH. Ajoute cette ligne à ~/.zshrc puis ouvre un nouveau terminal :"
     echo "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
echo "Étape suivante : approve --init"
