#!/bin/bash
# Builds the web app into ./data - every file gzipped, served by the firmware
# with Content-Encoding: gzip - and flashes it as a LittleFS image to the
# "ffat" data partition (offset 0x410000, 0xBE0000 bytes).
#
#   ./upload_fs.sh [port]         build + flash  (default /dev/cu.usbmodem101)
#   BUILD_ONLY=1 ./upload_fs.sh   build + size report only
#
# Sources: web/index.html and web/sw.js, MediaPipe from tracking/vendor
# (tracking/fetch_vendor.sh), and the hand_landmarker.task model.
# Close finger_test.py / any serial monitor before flashing.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-/dev/cu.usbmodem101}"
OFFSET=0x410000
SIZE=0xBE0000
ROOT=../..
MP="$ROOT/tracking/vendor/tasks-vision-1.0.1"
TOOLS="$HOME/Library/Arduino15/packages/esp32/tools"
MKLITTLEFS=$(ls "$TOOLS"/mklittlefs/*/mklittlefs | tail -1)
ESPTOOL=$(ls "$TOOLS"/esptool_py/*/esptool | tail -1)

[ -f "$MP/wasm/vision_wasm_internal.wasm" ] || "$ROOT/tracking/fetch_vendor.sh"

# source file : path on the hand. Only the SIMD wasm build is shipped; every
# current phone browser supports WebAssembly SIMD.
FILES=(
  "$ROOT/web/index.html:index.html"
  "$MP/vision_bundle.esm.js:mp/vision_bundle.esm.js"
  "$MP/wasm/vision_wasm_internal.js:mp/wasm/vision_wasm_internal.js"
  "$MP/wasm/vision_wasm_internal.wasm:mp/wasm/vision_wasm_internal.wasm"
  "$ROOT/hand_landmarker.task:mp/hand_landmarker.task"
)
VERSION=$(for f in "${FILES[@]}" "$ROOT/web/sw.js:"; do cat "${f%%:*}"; done | shasum | cut -c1-12)

STAGE=$(mktemp -d)
# The service worker's cache name carries the build version, so a new build
# replaces the cached copy on the phone.
sed "s/__BUILD__/$VERSION/" "$ROOT/web/sw.js" > "$STAGE/sw.js"
FILES+=("$STAGE/sw.js:sw.js")
# Uncompressed sizes, so the page can show download progress.
{
  printf '{"version":"%s","sizes":{' "$VERSION"
  sep=""
  for f in "${FILES[@]}"; do printf '%s"%s":%d' "$sep" "${f#*:}" "$(wc -c < "${f%%:*}")"; sep=","; done
  printf '}}\n'
} > "$STAGE/manifest.json"
FILES+=("$STAGE/manifest.json:manifest.json")

rm -rf data
raw=0
gz=0
for f in "${FILES[@]}"; do
  src="${f%%:*}"
  dst="data/${f#*:}.gz"
  mkdir -p "$(dirname "$dst")"
  gzip -9 -n -c "$src" > "$dst"
  r=$(wc -c < "$src")
  g=$(wc -c < "$dst")
  raw=$((raw + r))
  gz=$((gz + g))
  printf "  %-36s %10d -> %9d bytes\n" "${f#*:}" "$r" "$g"
done
rm -rf "$STAGE"
printf "  %-36s %10d -> %9d bytes = %.2f MB of the %.2f MB partition (%d%%)\n" "TOTAL" "$raw" "$gz" \
  "$(echo "$gz / 1048576" | bc -l)" "$(echo "$((SIZE)) / 1048576" | bc -l)" $((gz * 100 / SIZE))
echo "  build $VERSION"

[ "${BUILD_ONLY:-0}" = 1 ] && exit 0

IMG="${TMPDIR:-/tmp}/hand_littlefs.bin"
"$MKLITTLEFS" -c data -p 256 -b 4096 -s $((SIZE)) "$IMG"
"$ESPTOOL" --chip esp32s3 --port "$PORT" --baud 921600 write-flash "$OFFSET" "$IMG"
rm -f "$IMG"
