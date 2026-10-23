#!/usr/bin/env bash
# Обновить вендоренное ядро mesh из соседнего vsc_desktop (эталон протокола).
set -euo pipefail
D="${1:-../vsc_desktop/src}"
for f in __init__ packet tags wire link node; do cp "$D/mys_mesh/$f.py" mesh/mys_mesh/; done
cp "$D/mys_crypto/_ristretto.py" "$D/mys_crypto/linkcrypto.py" mesh/mys_crypto/
echo "mesh/ обновлён из $D"
