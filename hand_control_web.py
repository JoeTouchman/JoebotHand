"""Robotic Hand control panel (browser-based).

Run with:  python3 hand_control.py

It auto-connects to the Arduino and opens the control page in your
web browser. Ctrl-C to quit.

Calibration: open a finger's "Setup" panel and use the arrow buttons
to jog the servo to the finger's straight position (start) and fully
bent position (end). "Reverse" flips the direction for servos mounted
backwards. The main slider then sweeps 0-100% between your two points.
Calibration is saved to hand_cal.json and survives restarts.
"""

import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import serial
import serial.tools.list_ports

BAUD = 115200
PORT = 8000
FINGERS = ["Thumb", "Index", "Middle", "Ring", "Pinky"]
PINS = [3, 5, 6, 9, 10]
CAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hand_cal.json")

ser = None
ser_lock = threading.Lock()
# start/end are real servo degrees; start > end means the servo runs reversed
cal = [{"start": 0, "end": 180} for _ in FINGERS]
last_actual = [90] * len(FINGERS)  # last real angle sent (Arduino boots at 90)


def load_cal():
    global cal
    try:
        with open(CAL_FILE) as f:
            data = json.load(f)
        if (isinstance(data, list) and len(data) == len(FINGERS)
                and all("start" in d and "end" in d for d in data)):
            cal = [{"start": max(0, min(180, int(d["start"]))),
                    "end": max(0, min(180, int(d["end"])))} for d in data]
    except (OSError, ValueError, TypeError):
        pass


def save_cal():
    with open(CAL_FILE, "w") as f:
        json.dump(cal, f)


def find_arduino_port():
    for p in serial.tools.list_ports.comports():
        if any(tag in p.device for tag in ("usbmodem", "usbserial", "wchusbserial")):
            return p.device
    return None


def connect():
    global ser, last_actual
    port = find_arduino_port()
    if not port:
        return False
    try:
        ser = serial.Serial(port, BAUD, timeout=1)
        time.sleep(2.5)  # opening the port resets the Arduino; wait for boot
        last_actual = [90] * len(FINGERS)  # reset re-centers all servos
        return True
    except serial.SerialException:
        ser = None
        return False


def send_raw(idx, actual):
    """Send a real servo angle (0-180)."""
    global ser
    actual = max(0, min(180, int(round(actual))))
    with ser_lock:
        for attempt in range(2):  # one retry: reconnect if the write fails
            if ser is None and not connect():
                continue
            try:
                ser.write(f"{idx}:{actual}\n".encode())
                last_actual[idx] = actual
                return actual
            except serial.SerialException:
                ser = None
        return None


def send_percent(idx, t):
    """Send a slider position: 0% = start point, 100% = end point."""
    t = max(0.0, min(100.0, t))
    c = cal[idx]
    actual = c["start"] + t / 100.0 * (c["end"] - c["start"])
    return send_raw(idx, actual)


PAGE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Robotic Hand Control</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 640px;
         margin: 40px auto; padding: 0 20px; background: #f5f5f7; }
  h1 { font-size: 1.4em; }
  .finger { background: #fff; border-radius: 10px; padding: 14px 18px;
            margin: 10px 0; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
  .mainrow { display: flex; align-items: center; gap: 12px; }
  .mainrow label { width: 104px; font-weight: 600; }
  .mainrow input { flex: 1; }
  .mainrow .val { width: 44px; text-align: right; font-variant-numeric: tabular-nums; }
  .mainrow .real { width: 44px; font-size: .78em; color: #888; text-align: right; }
  .setup-toggle { padding: 6px 10px; border: 1px solid #bbb; border-radius: 6px;
                  background: #fff; color: #444; font-size: .85em; cursor: pointer; }
  .setup { display: none; margin-top: 12px; padding-top: 12px;
           border-top: 1px solid #eee; font-size: .9em; }
  .setup.open { display: block; }
  .cal-row { display: flex; align-items: center; gap: 6px; margin: 6px 0; }
  .cal-row .which { width: 46px; color: #555; }
  .cal-row .deg { width: 44px; text-align: center; font-weight: 600;
                  cursor: pointer; text-decoration: underline dotted; }
  .jog { width: 40px; padding: 5px 0; border: 1px solid #0071e3; border-radius: 6px;
         background: #fff; color: #0071e3; cursor: pointer; font-size: .9em; }
  .jog:active { background: #e8f1fd; }
  .rev { margin-left: 10px; padding: 5px 12px; border: 1px solid #b25000;
         border-radius: 6px; background: #fff; color: #b25000; cursor: pointer; }
  .rev:active { background: #fdf0e6; }
  .hint { color: #999; font-size: .82em; margin-top: 6px; }
  .presets { margin: 18px 0; display: flex; gap: 10px; }
  .presets button { flex: 1; padding: 10px; border: none; border-radius: 8px;
           background: #0071e3; color: #fff; font-size: 1em; cursor: pointer; }
  .presets button:active { background: #005bb5; }
  #status { margin-top: 14px; color: #666; }
  #status.err { color: #c00; font-weight: 600; }
</style>
</head>
<body>
<h1>🤖 Robotic Hand Control</h1>
<div id="sliders"></div>
<div class="presets">
  <button onclick="setAll(0)">All → start</button>
  <button onclick="setAll(50)">All → middle</button>
  <button onclick="setAll(100)">All → end</button>
</div>
<div id="status">Ready</div>
<script>
const FINGERS = %FINGERS%;
const PINS = %PINS%;
let cal = %CAL%;
const INITIAL = %INITIAL%;
const box = document.getElementById('sliders');
const statusEl = document.getElementById('status');

function realAngle(i, t) {
  return Math.round(cal[i].start + t / 100 * (cal[i].end - cal[i].start));
}

FINGERS.forEach((name, i) => {
  const div = document.createElement('div');
  div.className = 'finger';
  div.innerHTML = `
    <div class="mainrow">
      <label>${name} <small>(pin ${PINS[i]})</small></label>
      <input type="range" min="0" max="100" value="${INITIAL[i]}" id="s${i}"
             oninput="moved(${i}, this.value)">
      <span class="val" id="v${i}">${INITIAL[i]}%</span>
      <span class="real" id="r${i}">${realAngle(i, INITIAL[i])}°</span>
      <button class="setup-toggle" onclick="toggleSetup(${i})">Setup</button>
    </div>
    <div class="setup" id="setup${i}">
      <div class="cal-row">
        <span class="which">Start</span>
        <button class="jog" onclick="jog(${i},'start',-10)">−10</button>
        <button class="jog" onclick="jog(${i},'start',-1)">−1</button>
        <span class="deg" id="cs${i}" title="Click to move servo here"
              onclick="gotoPoint(${i},'start')">${cal[i].start}°</span>
        <button class="jog" onclick="jog(${i},'start',1)">+1</button>
        <button class="jog" onclick="jog(${i},'start',10)">+10</button>
      </div>
      <div class="cal-row">
        <span class="which">End</span>
        <button class="jog" onclick="jog(${i},'end',-10)">−10</button>
        <button class="jog" onclick="jog(${i},'end',-1)">−1</button>
        <span class="deg" id="ce${i}" title="Click to move servo here"
              onclick="gotoPoint(${i},'end')">${cal[i].end}°</span>
        <button class="jog" onclick="jog(${i},'end',1)">+1</button>
        <button class="jog" onclick="jog(${i},'end',10)">+10</button>
        <button class="rev" onclick="reverse(${i})">Reverse</button>
      </div>
      <div class="hint">Arrows move the servo live. Click a degree number to jump
        the servo to that point. Reverse swaps start and end.</div>
    </div>`;
  box.appendChild(div);
});

function toggleSetup(i) {
  document.getElementById('setup' + i).classList.toggle('open');
}

function refreshLabels(i) {
  const s = document.getElementById('s' + i);
  document.getElementById('v' + i).textContent = s.value + '%';
  document.getElementById('r' + i).textContent = realAngle(i, s.value) + '°';
  document.getElementById('cs' + i).textContent = cal[i].start + '°';
  document.getElementById('ce' + i).textContent = cal[i].end + '°';
}

function setStatus(ok, msg) {
  statusEl.textContent = msg;
  statusEl.className = ok ? '' : 'err';
}

let pending = {};
let inflight = false;

function moved(i, val) {
  document.getElementById('v' + i).textContent = val + '%';
  document.getElementById('r' + i).textContent = realAngle(i, val) + '°';
  pending[i] = val;
  flush();
}

async function flush() {
  if (inflight) return;
  const keys = Object.keys(pending);
  if (!keys.length) return;
  const i = keys[0], val = pending[i];
  delete pending[i];
  inflight = true;
  try {
    const r = await fetch(`/move?f=${i}&t=${val}`);
    const j = await r.json();
    setStatus(j.ok, j.ok ? 'Connected' : 'Arduino not found — is it plugged in?');
  } catch (e) {
    setStatus(false, 'Server stopped — rerun hand_control.py');
  }
  inflight = false;
  flush();
}

async function calSet(i, which, v) {
  try {
    const r = await fetch(`/cal/set?f=${i}&which=${which}&v=${v}`);
    const j = await r.json();
    if (!j.ok) { setStatus(false, 'Arduino not found — is it plugged in?'); return; }
    cal[i] = j.cal;
    // servo is now physically at that point, so snap the slider to match
    document.getElementById('s' + i).value = (which === 'start') ? 0 : 100;
    refreshLabels(i);
    setStatus(true, `${FINGERS[i]} ${which} point: ${v}° (saved)`);
  } catch (e) {
    setStatus(false, 'Server stopped — rerun hand_control.py');
  }
}

function jog(i, which, delta) {
  const v = Math.max(0, Math.min(180, cal[i][which] + delta));
  calSet(i, which, v);
}

function gotoPoint(i, which) {
  calSet(i, which, cal[i][which]);
}

async function reverse(i) {
  try {
    const r = await fetch(`/cal/reverse?f=${i}`);
    const j = await r.json();
    if (!j.ok) return;
    cal[i] = j.cal;
    // same physical position now corresponds to the mirrored slider value
    const s = document.getElementById('s' + i);
    s.value = 100 - s.value;
    refreshLabels(i);
    setStatus(true, `${FINGERS[i]} direction reversed (saved)`);
  } catch (e) {
    setStatus(false, 'Server stopped — rerun hand_control.py');
  }
}

function setAll(t) {
  FINGERS.forEach((_, i) => {
    document.getElementById('s' + i).value = t;
    moved(i, t);
  });
}
</script>
</body>
</html>"""


def render_page():
    initial = []
    for i in range(len(FINGERS)):
        c = cal[i]
        span = c["end"] - c["start"]
        t = 50 if span == 0 else (last_actual[i] - c["start"]) / span * 100
        initial.append(max(0, min(100, int(round(t)))))
    return (PAGE
            .replace("%FINGERS%", json.dumps(FINGERS))
            .replace("%PINS%", json.dumps(PINS))
            .replace("%CAL%", json.dumps(cal))
            .replace("%INITIAL%", json.dumps(initial))
            .encode())


class Handler(BaseHTTPRequestHandler):
    def reply_json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            body = render_page()
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/move":
            try:
                idx = int(q["f"][0])
                t = float(q["t"][0])
                ok = 0 <= idx < len(FINGERS) and send_percent(idx, t) is not None
            except (KeyError, ValueError):
                ok = False
            self.reply_json({"ok": ok})
        elif url.path == "/cal/set":
            try:
                idx = int(q["f"][0])
                which = q["which"][0]
                v = max(0, min(180, int(float(q["v"][0]))))
                assert 0 <= idx < len(FINGERS) and which in ("start", "end")
            except (KeyError, ValueError, AssertionError):
                self.reply_json({"ok": False})
                return
            cal[idx][which] = v
            save_cal()
            ok = send_raw(idx, v) is not None  # jog the servo to the new point
            self.reply_json({"ok": ok, "cal": cal[idx]})
        elif url.path == "/cal/reverse":
            try:
                idx = int(q["f"][0])
                assert 0 <= idx < len(FINGERS)
            except (KeyError, ValueError, AssertionError):
                self.reply_json({"ok": False})
                return
            c = cal[idx]
            c["start"], c["end"] = c["end"], c["start"]
            save_cal()
            self.reply_json({"ok": True, "cal": c})
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass  # keep the terminal quiet


if __name__ == "__main__":
    load_cal()
    print("Calibration:",
          ", ".join(f"{FINGERS[i]} {cal[i]['start']}→{cal[i]['end']}°"
                    for i in range(len(FINGERS))))
    print("Connecting to Arduino...")
    if connect():
        print(f"Connected on {ser.port}")
    else:
        print("Arduino not found - will keep trying when you move a slider.")
    print(f"Control panel: http://localhost:{PORT}  (Ctrl-C to quit)")
    webbrowser.open(f"http://localhost:{PORT}")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye")
