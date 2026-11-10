#!/bin/bash
# Downloads the MediaPipe tasks-vision files and the hand landmark model into
# tracking/vendor/, so the pages work with no internet - e.g. while the
# laptop is on the hand's JOES-HAND WiFi. Run once while online.
# Used by tracking/hand_tracking.html, web/index.html (local testing),
# firmware/esp32_hand/upload_fs.sh and desktop/hand_control.py.
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
# Google's float16 hand landmarker model (~7.8 MB)
[ -f vendor/hand_landmarker.task ] || curl -fsSL \
  https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task \
  -o vendor/hand_landmarker.task
du -sh vendor
