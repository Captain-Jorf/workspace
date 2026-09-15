#!/usr/bin/env bash
# Fonts for the reel factory (both OFL, both free, fetched from the public npm registry):
#   fa-*.ttf  Vazirmatn  (full build: Persian + Latin + «» + ZWNJ)  — upstream package "vazirmatn"
#   en-*.ttf  Sora       (Latin)                                      — @fontsource/sora woff2 → ttf
# Fallback for Vazirmatn: @fontsource/vazirmatn arabic subset (what the first pipeline used).
# Idempotent: skips the download when all 10 files already exist.
set -euo pipefail
DIR="$(cd "$(dirname "$0")/../assets/fonts" 2>/dev/null && pwd || true)"
mkdir -p "$(dirname "$0")/../assets/fonts"
cd "$(dirname "$0")/../assets/fonts"

need=0
for w in 400 500 600 700 800; do
  [ -s "fa-$w.ttf" ] && [ -s "en-$w.ttf" ] || need=1
done
if [ "$need" = 0 ] && [ "${FORCE_FONTS:-}" != "1" ]; then
  echo "fonts already present"; exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- Vazirmatn (full)
if curl -fsSL --retry 3 --max-time 120 -o "$TMP/vz.tgz" https://registry.npmjs.org/vazirmatn/-/vazirmatn-33.0.3.tgz; then
  mkdir -p "$TMP/vz" && tar xzf "$TMP/vz.tgz" -C "$TMP/vz"
  cp "$TMP/vz/package/fonts/ttf/Vazirmatn-Regular.ttf"  fa-400.ttf
  cp "$TMP/vz/package/fonts/ttf/Vazirmatn-Medium.ttf"   fa-500.ttf
  cp "$TMP/vz/package/fonts/ttf/Vazirmatn-SemiBold.ttf" fa-600.ttf
  cp "$TMP/vz/package/fonts/ttf/Vazirmatn-Bold.ttf"     fa-700.ttf
  cp "$TMP/vz/package/fonts/ttf/Vazirmatn-ExtraBold.ttf" fa-800.ttf
  echo "Vazirmatn (full) ready"
else
  echo "::warning::full Vazirmatn download failed — falling back to @fontsource arabic subset"
  curl -fsSL --retry 3 --max-time 120 -o "$TMP/vzs.tgz" https://registry.npmjs.org/@fontsource/vazirmatn/-/vazirmatn-5.3.0.tgz
  mkdir -p "$TMP/vzs" && tar xzf "$TMP/vzs.tgz" -C "$TMP/vzs"
  python3 - "$TMP/vzs" <<'PY'
import os, sys
from fontTools.ttLib import TTFont
base = sys.argv[1]
for w in (400, 500, 600, 700, 800):
    src = f"{base}/package/files/vazirmatn-arabic-{w}-normal.woff2"
    if os.path.exists(src):
        f = TTFont(src); f.flavor = None; f.save(f"fa-{w}.ttf")
print("Vazirmatn (subset) ready")
PY
fi

# --- Sora (Latin)
curl -fsSL --retry 3 --max-time 120 -o "$TMP/sora.tgz" https://registry.npmjs.org/@fontsource/sora/-/sora-5.3.0.tgz
mkdir -p "$TMP/sr" && tar xzf "$TMP/sora.tgz" -C "$TMP/sr"
python3 - "$TMP/sr" <<'PY'
import os, sys
from fontTools.ttLib import TTFont
base = sys.argv[1]
for w in (400, 500, 600, 700, 800):
    src = f"{base}/package/files/sora-latin-{w}-normal.woff2"
    if os.path.exists(src):
        f = TTFont(src); f.flavor = None; f.save(f"en-{w}.ttf")
print("Sora ready")
PY

for w in 400 500 600 700 800; do
  [ -s "fa-$w.ttf" ] && [ -s "en-$w.ttf" ] || { echo "::error::font fa-$w/en-$w missing"; exit 1; }
done
echo "fonts ready"
