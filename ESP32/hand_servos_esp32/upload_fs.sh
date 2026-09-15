#!/bin/bash
# Builds a LittleFS image from ./data and flashes it to the ESP32's data
# partition ("ffat": offset 0x410000, size 0xBE0000 in the fatflash
# partition table). Run this whenever the web page changes; the sketch
# itself is still uploaded with arduino-cli.
#
# Close finger_test.py / any serial monitor first - this needs the port.
#
# Usage: ./upload_fs.sh [port]        (default /dev/cu.usbmodem101)
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-/dev/cu.usbmodem101}"
OFFSET=0x410000
SIZE=0xBE0000
TOOLS="$HOME/Library/Arduino15/packages/esp32/tools"
MKLITTLEFS=$(ls "$TOOLS"/mklittlefs/*/mklittlefs | tail -1)
ESPTOOL=$(ls "$TOOLS"/esptool_py/*/esptool | tail -1)
IMG="${TMPDIR:-/tmp}/hand_littlefs.bin"

"$MKLITTLEFS" -c data -p 256 -b 4096 -s $((SIZE)) "$IMG"
"$ESPTOOL" --chip esp32s3 --port "$PORT" --baud 921600 write-flash "$OFFSET" "$IMG"
rm -f "$IMG"
