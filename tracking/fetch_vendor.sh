#!/bin/bash
# Downloads the MediaPipe tasks-vision files the harness needs into
# tracking/vendor/, so the page works with no internet - e.g. while the
# laptop is on the hand's JOES-HAND WiFi. Run once while online.
set -euo pipefail
cd "$(dirname "$0")"

V=1.0.1
CDN="https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@$V"
DEST="vendor/tasks-vision-$V"
mkdir -p "$DEST/wasm"

# Saved with a .js extension so python's http.server sends a JavaScript MIME type.
curl -fsSL "$CDN/vision_bundle.mjs" -o "$DEST/vision_bundle.esm.js"
for f in vision_wasm_internal.js vision_wasm_internal.wasm \
         vision_wasm_nosimd_internal.js vision_wasm_nosimd_internal.wasm; do
  curl -fsSL "$CDN/wasm/$f" -o "$DEST/wasm/$f"
done
du -sh "$DEST"
