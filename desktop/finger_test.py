"""Five-finger test controller for the ESP32-S3 hand (browser-based).

Run with:  python3 finger_test.py

Opens a page with one slider per finger: 0% = fully open, 100% = fully
closed. The sliders send percentages; the ESP32 firmware
(firmware/esp32_hand) holds each finger's calibrated open/closed servo
angles and clamps to them, so every slider means "how curled is this
finger" the same way. Ctrl-C to quit.
"""

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import serial
import serial.tools.list_ports

BAUD = 115200
HTTP_PORT = 8000
FINGERS = ["Thumb", "Index", "Middle", "Ring", "Pinky"]
PINS = [4, 5, 6, 7, 15]

ser = None
ser_lock = threading.Lock()


def find_port():
    ports = [p.device for p in serial.tools.list_ports.comports()]
    for tag in ("usbmodem", "usbserial", "wchusbserial"):  # ESP32 native USB first
        for dev in ports:
            if tag in dev:
                return dev
    return None


def connect():
    global ser
    port = find_port()
    if not port:
        return False
    try:
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = port, BAUD, 1
        s.dtr = s.rts = False  # don't touch the ESP32's reset/boot lines
        s.open()
        ser = s
        return True
    except serial.SerialException:
        ser = None
        return False


def send_line(line):
    global ser
    with ser_lock:
        for _ in range(2):  # one retry: reconnect if the write fails
            if ser is None and not connect():
                continue
            try:
                ser.write((line + "\n").encode())
                ser.reset_input_buffer()  # discard firmware chatter
                return True
            except (serial.SerialException, OSError):
                ser = None
        return False


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Finger Test</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 600px;
         margin: 40px auto; padding: 0 20px; background: #f5f5f7; }
  h1 { font-size: 1.3em; }
  .finger { display: flex; align-items: center; gap: 12px; background: #fff;
            border-radius: 10px; padding: 14px 18px; margin: 10px 0;
            box-shadow: 0 1px 3px rgba(0,0,0,.1); }
  .finger label { width: 120px; font-weight: 600; }
  .finger input { flex: 1; }
  .val { width: 44px; text-align: right; font-variant-numeric: tabular-nums; }
  .buttons { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }
  .buttons button { flex: 1; padding: 10px; border: none; border-radius: 8px;
                    background: #0071e3; color: #fff; font-size: 1em; cursor: pointer; }
  .buttons button.anim { background: #34a853; }
  .buttons button.relax { background: #8e8e93; }
  #status { color: #666; }
  #status.err { color: #c00; font-weight: 600; }
</style>
</head>
<body>
<h1>Finger Test</h1>
<p style="color:#888">0% = fully open, 100% = fully curled. Calibration lives in the firmware.</p>
<div id="sliders"></div>
<div class="buttons">
  <button onclick="setAll(0)">All open</button>
  <button onclick="setAll(50)">All half</button>
  <button onclick="setAll(100)">All closed</button>
</div>
<div class="buttons">
  <button class="anim" onclick="anim()">Play animation</button>
  <button class="relax" onclick="relax()">Relax all</button>
</div>
<div id="status">Ready</div>
<script>
const FINGERS = %FINGERS%, PINS = %PINS%;
const statusEl = document.getElementById('status');

FINGERS.forEach((name, i) => {
  const d = document.createElement('div');
  d.className = 'finger';
  d.innerHTML = `<label>${name} <small>(GPIO ${PINS[i]})</small></label>
    <input type="range" min="0" max="100" value="0" id="s${i}" oninput="moved(${i}, this.value)">
    <span class="val" id="v${i}">0%</span>`;
  document.getElementById('sliders').appendChild(d);
});

function setStatus(ok, msg) { statusEl.textContent = msg; statusEl.className = ok ? '' : 'err'; }
function show(i, t) {
  document.getElementById('s' + i).value = t;
  document.getElementById('v' + i).textContent = t + '%';
}

let pending = {}, inflight = false;
function moved(i, t) {
  show(i, t);
  pending[i] = t;
  flush();
}
async function call(url, okMsg) {
  try {
    const j = await (await fetch(url)).json();
    setStatus(j.ok, j.ok ? (okMsg || 'Connected') : 'ESP32 not found — is it plugged in?');
    return j.ok;
  } catch (e) { setStatus(false, 'Server stopped — rerun finger_test.py'); return false; }
}
async function flush() {
  if (inflight) return;
  const keys = Object.keys(pending);
  if (!keys.length) return;
  const i = keys[0], t = pending[i];
  delete pending[i];
  inflight = true;
  await call(`/move?f=${i}&t=${t}`);
  inflight = false;
  flush();
}
function setAll(t) { FINGERS.forEach((_, i) => moved(i, t)); }
async function anim() {
  // the animation ends with every finger open
  if (await call('/anim', 'Playing startup animation…')) FINGERS.forEach((_, i) => show(i, 0));
}
function relax() { call('/relax', 'Relaxed — servos limp until the next move'); }
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def reply(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            page = (PAGE.replace("%FINGERS%", json.dumps(FINGERS))
                        .replace("%PINS%", json.dumps(PINS)))
            self.reply(page.encode(), "text/html; charset=utf-8")
            return
        if url.path == "/move":
            try:
                idx = int(q["f"][0])
                pct = max(0.0, min(100.0, float(q["t"][0])))
                ok = 0 <= idx < len(FINGERS) and send_line(f"{idx}:{pct:g}")
            except (KeyError, ValueError):
                ok = False
        elif url.path == "/anim":
            ok = send_line("A")
        elif url.path == "/relax":
            ok = send_line("R")
        else:
            self.send_error(404)
            return
        self.reply(json.dumps({"ok": ok}).encode(), "application/json")

    def log_message(self, *args):
        pass  # keep the terminal quiet


if __name__ == "__main__":
    for name, pin in zip(FINGERS, PINS):
        print(f"  {name:6s} GPIO {pin:2d}")
    print(f"Connected on {ser.port}" if connect() else
          "ESP32 not found - will keep trying when you move a slider.")
    print(f"Control page: http://localhost:{HTTP_PORT}  (Ctrl-C to quit)")
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), Handler)
    webbrowser.open(f"http://localhost:{HTTP_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye")
