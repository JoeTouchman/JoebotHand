// Standalone pin diagnostic: one servo attached at a time, on each
// soldered pin in turn. Watch which physical finger moves while each
// "testing GPIO n" line is showing.

#include <ESP32Servo.h>

int pins[5] = {4, 5, 6, 7, 15};

// Sweep range in degrees. 50-135 lies inside EVERY finger's calibrated
// range in desktop/hand_cal.json, so whichever finger a pin turns out to drive,
// it can't be pushed into a hard stop. Set to 0 and 180 for a full sweep.
const int SWEEP_MIN = 50;
const int SWEEP_MAX = 135;
const int SWEEP_MS = 5000;  // one full min -> max -> min cycle

Servo s;

void setup() {
  Serial.begin(115200);
  delay(2000);
}

void loop() {
  int stepDelay = SWEEP_MS / (2 * (SWEEP_MAX - SWEEP_MIN));
  for (int i = 0; i < 5; i++) {
    s.setPeriodHertz(50);
    int ch = s.attach(pins[i], 544, 2400);
    Serial.printf("testing GPIO %d (lib channel %d)\n", pins[i], ch);
    for (int a = SWEEP_MIN; a <= SWEEP_MAX; a++) {
      s.write(a);
      delay(stepDelay);
    }
    for (int a = SWEEP_MAX; a >= SWEEP_MIN; a--) {
      s.write(a);
      delay(stepDelay);
    }
    s.detach();
    // ESP32Servo 3.2.1 leaves the pin wired to the MCPWM output after
    // detach(); drive it low as a plain GPIO so only one pin ever gets pulses.
    pinMode(pins[i], OUTPUT);
    digitalWrite(pins[i], LOW);
    delay(1000);
  }
}
