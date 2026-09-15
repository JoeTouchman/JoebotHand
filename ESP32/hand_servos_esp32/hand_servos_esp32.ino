// Robotic Hand - 5 servo controller (ESP32-S3 port of hand_servos.ino)
//
// Wiring (servo signal wires):
//   Thumb  -> GPIO 4
//   Index  -> GPIO 5
//   Middle -> GPIO 6
//   Ring   -> GPIO 7
//   Pinky  -> GPIO 15
//
// Servo power: separate 5V supply (red -> 5V, brown -> GND), with the
// supply's GND tied to the ESP32's GND. Never the ESP32's 5V pin.
//
// Servos are driven directly with the core's LEDC PWM. Do not use
// ESP32Servo in this project: on the S3 (v3.2.1) it routes several
// servos to the same MCPWM output.
//
// LEDC budget: the S3 has 8 channels. These 5 servos use 5 (sharing one
// timer), leaving 3. Planned: status LED (PWM) and buzzer (tone() also
// takes a channel).

#include "esp32-hal-periman.h"
#include "soc/gpio_reg.h"
#include "soc/gpio_sig_map.h"
#include <WiFi.h>
#include <esp_wifi.h>
#include <LittleFS.h>
#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>

const uint32_t SERVO_FREQ = 50;      // Hz
const uint32_t PERIOD_US = 20000;    // 1 / 50 Hz
// 14 bits is the S3's LEDC maximum (ledcAttach rejects 16).
// At 50 Hz that's 20000 / 16384 = ~1.22 us per step.
const uint8_t SERVO_RES = 14;
static_assert(SERVO_RES <= SOC_LEDC_TIMER_BIT_WIDTH, "LEDC resolution too high for this chip");

// Same pulse range as the Arduino Servo library (544-2400us), so servo
// degrees mean the same physical positions they did on the Uno.
const int PULSE_MIN = 544;
const int PULSE_MAX = 2400;

// --- calibration -------------------------------------------------------
// The single per-finger calibration table: servo angle at fully open and
// at fully closed. openAngle > closedAngle means the linkage runs
// backwards - that's intended, don't normalize it. The finger is never
// driven outside [openAngle, closedAngle].
//
// Measured 2026-09-15 as slider % on the old hand_cal.json scale
// (start -> end), converted with angle = start + pct * (end - start).
struct FingerCal { int pin; int openAngle; int closedAngle; };

const int NUM_FINGERS = 5;
const FingerCal FINGERS[NUM_FINGERS] = {
  {4,  135,  10},  // Thumb:  0-100% of 135->10  (unchanged)
  {5,  144,   0},  // Index:  20-100% of 180->0
  {6,   36, 180},  // Middle: 20-100% of 0->180
  {7,   50, 154},  // Ring:   0-80% of 50->180 (past 154 over-rotates)
  {15,  16, 160},  // Pinky:  10-100% of 0->160
};

bool attachedOk[NUM_FINGERS];
float fingerPct[NUM_FINGERS];  // last commanded position, 0.0-1.0

// --- low level: only setFinger() calls these -------------------------
void writePulseUs(int pin, uint32_t pulse_us) {
  uint32_t duty = (pulse_us * (1UL << SERVO_RES)) / PERIOD_US;
  ledcWrite(pin, duty);
}

uint32_t angleToPulseUs(float angle) {
  float us = PULSE_MIN + angle * (PULSE_MAX - PULSE_MIN) / 180.0f;
  return (uint32_t)constrain(lroundf(us), (long)PULSE_MIN, (long)PULSE_MAX);
}

// The one way to move a finger. pct 0.0 = open, 1.0 = closed.
void setFinger(int index, float pct) {
  if (index < 0 || index >= NUM_FINGERS || isnan(pct)) return;
  pct = constrain(pct, 0.0f, 1.0f);
  const FingerCal &f = FINGERS[index];
  float angle = f.openAngle + pct * (f.closedAngle - f.openAngle);
  // Absolute clamp to the calibrated range, whichever way it runs.
  float lo = min(f.openAngle, f.closedAngle);
  float hi = max(f.openAngle, f.closedAngle);
  angle = constrain(angle, lo, hi);
  writePulseUs(f.pin, angleToPulseUs(angle));
  fingerPct[index] = pct;
}

// Stops pulses on every servo (limp). Commands no position; the channels
// stay attached and the next setFinger() drives the servo again.
void relaxAll() {
  for (int i = 0; i < NUM_FINGERS; i++) ledcWrite(FINGERS[i].pin, 0);
}

// --- startup animation -------------------------------------------------
// All timings in one place so they're easy to tune. Replay with "A".
const int FRAME_MS = 10;          // update interval while animating
const int OPEN_MOVE_MS = 300;     // 1. all fingers to open...
const int OPEN_HOLD_MS = 300;     //    ...then pause
const int CURL_STAGGER_MS = 150;  // 2. delay between each finger's start
const int CURL_MS = 350;          //    time for one finger's full curl
const int CLOSED_HOLD_MS = 250;   //    hold with the fist closed
const int UNCURL_HOLD_MS = 150;   // 3. pause after uncurling
const int WAVE_STAGGER_MS = 90;   // 4. ripple: delay between fingers
const int WAVE_MS = 500;          //    one finger's curl-and-back
const float WAVE_DEPTH = 1.0f;    //    how far each finger curls (0-1)
const int END_HOLD_MS = 400;      // 5. final hold, open

const int THUMB_TO_PINKY[NUM_FINGERS] = {0, 1, 2, 3, 4};
const int PINKY_TO_THUMB[NUM_FINGERS] = {4, 3, 2, 1, 0};

// Ease in and out: zero speed at both ends of a move.
float smoothstep(float t) {
  t = constrain(t, 0.0f, 1.0f);
  return t * t * (3.0f - 2.0f * t);
}

// Move the fingers, in `order`, from wherever they are to `target`.
// Each finger starts `staggerMs` after the previous one and takes
// `durMs` to arrive.
void rippleTo(const int order[], float target, int staggerMs, int durMs) {
  float from[NUM_FINGERS];
  for (int i = 0; i < NUM_FINGERS; i++) from[i] = fingerPct[i];
  int total = staggerMs * (NUM_FINGERS - 1) + durMs;
  uint32_t t0 = millis();
  while (true) {
    int t = millis() - t0;
    for (int k = 0; k < NUM_FINGERS; k++) {
      int f = order[k];
      float u = smoothstep((t - k * staggerMs) / (float)durMs);
      setFinger(f, from[f] + (target - from[f]) * u);
    }
    if (t >= total) break;
    delay(FRAME_MS);
  }
}

// Each finger curls to `depth` and back to open, starting `staggerMs`
// after the previous one, so the curls overlap into a ripple.
void waveOnce(const int order[], float depth, int staggerMs, int durMs) {
  int total = staggerMs * (NUM_FINGERS - 1) + durMs;
  uint32_t t0 = millis();
  while (true) {
    int t = millis() - t0;
    for (int k = 0; k < NUM_FINGERS; k++) {
      float u = constrain((t - k * staggerMs) / (float)durMs, 0.0f, 1.0f);
      setFinger(order[k], depth * 0.5f * (1.0f - cosf(2.0f * PI * u)));
    }
    if (t >= total) break;
    delay(FRAME_MS);
  }
}

void playStartupAnimation() {
  uint32_t start = millis();
  // 1. all fingers to fully open, pause
  rippleTo(THUMB_TO_PINKY, 0.0f, 0, OPEN_MOVE_MS);
  delay(OPEN_HOLD_MS);
  // 2. curl thumb through pinky, one after another, brief hold
  rippleTo(THUMB_TO_PINKY, 1.0f, CURL_STAGGER_MS, CURL_MS);
  delay(CLOSED_HOLD_MS);
  // 3. uncurl pinky back to thumb
  rippleTo(PINKY_TO_THUMB, 0.0f, CURL_STAGGER_MS, CURL_MS);
  delay(UNCURL_HOLD_MS);
  // 4. wave: overlapping curl-and-release, thumb to pinky
  waveOnce(THUMB_TO_PINKY, WAVE_DEPTH, WAVE_STAGGER_MS, WAVE_MS);
  // 5. all fully open and hold
  for (int i = 0; i < NUM_FINGERS; i++) setFinger(i, 0.0f);
  delay(END_HOLD_MS);
  Serial.printf("animation done in %lu ms\n", (unsigned long)(millis() - start));
}

// --- info --------------------------------------------------------------
// Calibration, the LEDC channel the core assigned, and which LEDC channel
// the GPIO matrix actually routes to the pin.
void printInfo() {
  Serial.println("hand ready");
  int channels[NUM_FINGERS];
  for (int i = 0; i < NUM_FINGERS; i++) {
    int pin = FINGERS[i].pin;
    ledc_channel_handle_t *bus = (ledc_channel_handle_t *)perimanGetPinBus(pin, ESP32_BUS_TYPE_LEDC);
    int sig = REG_READ(GPIO_FUNC0_OUT_SEL_CFG_REG + 4 * pin) & 0x1FF;
    channels[i] = bus ? bus->channel : -1;
    if (!attachedOk[i] || !bus) {
      Serial.printf("  finger %d: GPIO %2d NOT ATTACHED\n", i, pin);
      continue;
    }
    Serial.printf("  finger %d: GPIO %2d, open %3d -> closed %3d deg, LEDC ch %d (timer %d), routed: ",
                  i, pin, FINGERS[i].openAngle, FINGERS[i].closedAngle, bus->channel, bus->timer_num);
    if (sig >= LEDC_LS_SIG_OUT0_IDX && sig <= LEDC_LS_SIG_OUT7_IDX) {
      Serial.printf("LEDC ch %d\n", sig - LEDC_LS_SIG_OUT0_IDX);
    } else {
      Serial.printf("signal %d (not LEDC!)\n", sig);
    }
  }
  bool distinct = true;
  for (int i = 0; i < NUM_FINGERS; i++)
    for (int j = i + 1; j < NUM_FINGERS; j++)
      if (channels[i] < 0 || channels[i] == channels[j]) distinct = false;
  Serial.println(distinct ? "  all 5 channels distinct" : "  WARNING: channels missing or shared");
}

// --- WiFi / web UI -----------------------------------------------------
// The ESP32 runs its own access point (it never joins another network)
// and serves data/index.html from LittleFS (flash it with upload_fs.sh).
// The phone talks over a websocket at /ws:
//   phone -> ESP32: "p0,p1,p2,p3,p4"  % curled 0-100, thumb..pinky. Sent
//                                      at most ~30 Hz while sliding, and
//                                      every 500 ms as a keepalive
//                   "A"               replay the startup animation
//   ESP32 -> phone: "p0,p1,p2,p3,p4"  the hand's actual position: on
//                                      connect, after an animation, and
//                                      when the watchdog opens the hand
// Websocket callbacks run on the network task, so they only store the
// newest frame; loop() is the only place that moves servos.
const char *AP_SSID = "JOES-HAND";
const char *AP_PASS = "joebotics";   // WPA2 needs at least 8 characters
const char *FS_PARTITION = "ffat";   // the fatflash scheme's 12 MB data partition
const uint32_t WATCHDOG_MS = 2000;   // open the hand after this long without a phone message

AsyncWebServer server(80);
AsyncWebSocket ws("/ws");
bool fsOk = false;
size_t pageBytes = 0;

portMUX_TYPE wsMux = portMUX_INITIALIZER_UNLOCKED;
int wsFrame[NUM_FINGERS];            // newest frame from the phone, 0-100
volatile bool wsFrameNew = false;
volatile bool wsAnimRequested = false;
volatile uint32_t lastWsMs = 0;
bool wsHolding = false;              // phone has set the fingers since the last watchdog trip

void stateString(char *out, size_t n) {
  snprintf(out, n, "%ld,%ld,%ld,%ld,%ld",
           lroundf(fingerPct[0] * 100), lroundf(fingerPct[1] * 100), lroundf(fingerPct[2] * 100),
           lroundf(fingerPct[3] * 100), lroundf(fingerPct[4] * 100));
}

void broadcastState() {
  char buf[32];
  stateString(buf, sizeof buf);
  ws.textAll(buf);
}

// Plays the animation, drops any phone frames that queued up during it,
// and tells the phone the hand is open again.
void runAnimation() {
  playStartupAnimation();
  wsFrameNew = false;
  broadcastState();
}

void onWsEvent(AsyncWebSocket *srv, AsyncWebSocketClient *client, AwsEventType type,
               void *arg, uint8_t *data, size_t len) {
  switch (type) {
    case WS_EVT_CONNECT: {
      Serial.printf("ws: client #%lu connected from %s (%u connected)\n", (unsigned long)client->id(),
                    client->remoteIP().toString().c_str(), (unsigned)srv->count());
      char buf[32];
      stateString(buf, sizeof buf);
      client->text(buf);
      break;
    }
    case WS_EVT_DISCONNECT:
      Serial.printf("ws: client #%lu disconnected\n", (unsigned long)client->id());
      break;
    case WS_EVT_DATA: {
      AwsFrameInfo *info = (AwsFrameInfo *)arg;
      // only small, unfragmented text messages
      if (!info->final || info->index != 0 || info->len != len || info->opcode != WS_TEXT
          || len == 0 || len > 31) return;
      char buf[32];
      memcpy(buf, data, len);
      buf[len] = '\0';
      if (strcmp(buf, "A") == 0) {
        lastWsMs = millis();
        wsAnimRequested = true;
        return;
      }
      int v[NUM_FINGERS], used = 0;
      if (sscanf(buf, "%d,%d,%d,%d,%d%n", &v[0], &v[1], &v[2], &v[3], &v[4], &used) != 5
          || used != (int)len) return;
      for (int i = 0; i < NUM_FINGERS; i++)
        if (v[i] < 0 || v[i] > 100) return;
      portENTER_CRITICAL(&wsMux);
      memcpy(wsFrame, v, sizeof wsFrame);
      wsFrameNew = true;
      lastWsMs = millis();
      portEXIT_CRITICAL(&wsMux);
      break;
    }
    default:
      break;
  }
}

void onWiFiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  switch (event) {
    case ARDUINO_EVENT_WIFI_AP_STACONNECTED: {
      const uint8_t *m = info.wifi_ap_staconnected.mac;
      Serial.printf("wifi: device %02x:%02x:%02x:%02x:%02x:%02x joined\n", m[0], m[1], m[2], m[3], m[4], m[5]);
      break;
    }
    case ARDUINO_EVENT_WIFI_AP_STAIPASSIGNED:
      Serial.printf("wifi: gave out %s\n", IPAddress(info.wifi_ap_staipassigned.ip.addr).toString().c_str());
      break;
    case ARDUINO_EVENT_WIFI_AP_STADISCONNECTED: {
      const uint8_t *m = info.wifi_ap_stadisconnected.mac;
      Serial.printf("wifi: device %02x:%02x:%02x:%02x:%02x:%02x left\n", m[0], m[1], m[2], m[3], m[4], m[5]);
      break;
    }
    default:
      break;
  }
}

void printNetworkStatus() {
  wifi_config_t cfg;
  esp_wifi_get_config(WIFI_IF_AP, &cfg);
  Serial.printf("wifi: AP \"%s\" %s, %s, channel %d, IP %s, %d device(s) joined, station mode %s\n",
                AP_SSID, (WiFi.getMode() & WIFI_AP) ? "up" : "DOWN",
                cfg.ap.authmode == WIFI_AUTH_WPA2_PSK ? "WPA2-PSK" : "NOT WPA2",
                cfg.ap.channel, WiFi.softAPIP().toString().c_str(), WiFi.softAPgetStationNum(),
                (WiFi.getMode() & WIFI_STA) ? "ON (unexpected!)" : "off");
  if (fsOk) {
    Serial.printf("fs: LittleFS on '%s' mounted, %u of %u KB used, index.html %s\n", FS_PARTITION,
                  (unsigned)(LittleFS.usedBytes() / 1024), (unsigned)(LittleFS.totalBytes() / 1024),
                  pageBytes ? (String(pageBytes) + " bytes").c_str() : "MISSING - run upload_fs.sh");
  } else {
    Serial.printf("fs: LittleFS mount on '%s' FAILED - run upload_fs.sh\n", FS_PARTITION);
  }
  Serial.printf("ws: %u client(s), watchdog %s\n", (unsigned)ws.count(), wsHolding ? "armed" : "idle");
}

void startNetwork() {
  WiFi.persistent(false);  // don't store WiFi settings in flash
  WiFi.onEvent(onWiFiEvent);
  WiFi.mode(WIFI_AP);      // access point only: never joins another network
  if (!WiFi.softAP(AP_SSID, AP_PASS, 1, 0, 4, false, WIFI_AUTH_WPA2_PSK, WIFI_CIPHER_TYPE_CCMP)) {
    Serial.println("wifi: softAP FAILED to start");
  }

  fsOk = LittleFS.begin(false, "/littlefs", 10, FS_PARTITION);
  if (fsOk) {
    File f = LittleFS.open("/index.html", "r");
    if (f) {
      pageBytes = f.size();
      f.close();
    }
  }

  ws.onEvent(onWsEvent);
  server.addHandler(&ws);
  if (pageBytes) {
    server.serveStatic("/", LittleFS, "/").setDefaultFile("index.html").setCacheControl("no-store");
  } else {
    server.on("/", HTTP_GET, [](AsyncWebServerRequest *req) {
      req->send(500, "text/plain", "index.html is not on the ESP32 yet - run upload_fs.sh");
    });
  }
  server.onNotFound([](AsyncWebServerRequest *req) { req->send(404, "text/plain", "not found"); });
  server.begin();

  printNetworkStatus();
  Serial.println("http: serving http://192.168.4.1/ (websocket /ws)");
}

void setup() {
  Serial.begin(115200);
  // Attach all five permanently and drive each straight to open. A fresh
  // channel outputs no pulses until the first write, so the first pulse
  // each servo sees is its open position.
  for (int i = 0; i < NUM_FINGERS; i++) {
    attachedOk[i] = ledcAttach(FINGERS[i].pin, SERVO_FREQ, SERVO_RES);
    if (attachedOk[i]) setFinger(i, 0.0f);
  }
  printInfo();
  startNetwork();
  playStartupAnimation();
}

// Serial protocol:
//   "2:37.5\n" -> finger 2 (middle) to 37.5% curled (0 = open, 100 = closed)
//   "A\n"      -> replay the startup animation
//   "R\n"      -> relax: stop pulses on all servos (limp)
//   "?\n"      -> print calibration, LEDC channels, routing and network status
void handleSerial() {
  String line = Serial.readStringUntil('\n');
  line.trim();

  if (line == "A") {
    runAnimation();
    return;
  }
  if (line == "R") {
    relaxAll();
    Serial.println("relaxed");
    return;
  }
  if (line == "?") {
    printInfo();
    printNetworkStatus();
    return;
  }

  int colon = line.indexOf(':');
  if (colon <= 0) return;
  int idx = line.substring(0, colon).toInt();
  float pct = line.substring(colon + 1).toFloat();
  setFinger(idx, pct / 100.0f);
}

void loop() {
  if (Serial.available()) handleSerial();

  // Newest phone frame -> fingers.
  if (wsFrameNew) {
    int v[NUM_FINGERS];
    portENTER_CRITICAL(&wsMux);
    memcpy(v, wsFrame, sizeof v);
    wsFrameNew = false;
    portEXIT_CRITICAL(&wsMux);
    for (int i = 0; i < NUM_FINGERS; i++) setFinger(i, v[i] / 100.0f);
    wsHolding = true;
  }

  if (wsAnimRequested) {
    wsAnimRequested = false;
    runAnimation();
  }

  // Watchdog: the phone sends at least every 500 ms while its page is
  // open. If it goes quiet for 2 s (locked, backgrounded, out of range,
  // or the socket dropped), open the hand instead of freezing mid-grip.
  // Read lastWsMs before millis() so a message landing in between can't
  // make the difference wrap around.
  uint32_t last = lastWsMs;
  if (wsHolding && millis() - last > WATCHDOG_MS) {
    wsHolding = false;
    for (int i = 0; i < NUM_FINGERS; i++) setFinger(i, 0.0f);
    Serial.println("watchdog: no phone message for 2 s -> all fingers open");
    broadcastState();
  }

  static uint32_t lastCleanup = 0;
  if (millis() - lastCleanup > 1000) {
    lastCleanup = millis();
    ws.cleanupClients();
  }
  delay(1);
}
