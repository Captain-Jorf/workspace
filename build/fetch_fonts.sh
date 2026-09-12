#!/usr/bin/env bash
# Downloads Vazirmatn (Persian) + Sora (Latin) woff2 from npm and converts to TTF.
set -e
cd "$(dirname "$0")/../assets/fonts"
curl -sS -o /tmp/vazir.tgz https://registry.npmjs.org/@fontsource/vazirmatn/-/vazirmatn-5.3.0.tgz
curl -sS -o /tmp/sora.tgz  https://registry.npmjs.org/@fontsource/sora/-/sora-5.3.0.tgz
mkdir -p /tmp/vz /tmp/sr && tar xzf /tmp/vazir.tgz -C /tmp/vz && tar xzf /tmp/sora.tgz -C /tmp/sr
python3 - <<'PY'
from fontTools.ttLib import TTFont
import os
for pat, out in (( "/tmp/vz/package/files/vazirmatn-arabic-%d-normal.woff2", "fa-%d.ttf"),
                 ( "/tmp/sr/package/files/sora-latin-%d-normal.woff2", "en-%d.ttf")):
    for w in (400, 500, 600, 700, 800):
        if os.path.exists(pat % w):
            f = TTFont(pat % w); f.flavor = None; f.save(out % w)
print("fonts ready")
PY
