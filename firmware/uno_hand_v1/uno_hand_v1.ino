// Robotic Hand - 5 servo controller
// Upload this with the Arduino IDE (board: Arduino Uno)
//
// Wiring (servo signal wires):
//   Thumb  -> pin 3
//   Index  -> pin 5
//   Middle -> pin 6
//   Ring   -> pin 9
//   Pinky  -> pin 10
//
// Servo power: red -> 5V, brown/black -> GND.
// If servos jitter or the board resets, power the servos from a
// separate 5V supply and connect its GND to the Arduino GND.

#include <Servo.h>

const int NUM_SERVOS = 5;
const int SERVO_PINS[NUM_SERVOS] = {3, 5, 6, 9, 10};

Servo servos[NUM_SERVOS];
bool attached[NUM_SERVOS] = {false, false, false, false, false};

void setup() {
  Serial.begin(115200);
  // Servos stay detached (limp, low power draw) until first commanded.
  // No snap-to-center on boot, and unmoved fingers don't load the 5V rail.
}

// Protocol: lines like "2:135\n"  ->  servo index 2 (middle) to 135 degrees
void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    int colon = line.indexOf(':');
    if (colon > 0) {
      int idx = line.substring(0, colon).toInt();
      int angle = line.substring(colon + 1).toInt();
      if (idx >= 0 && idx < NUM_SERVOS) {
        angle = constrain(angle, 0, 180);
        servos[idx].write(angle);  // set target before attach: no snap to default
        if (!attached[idx]) {
          servos[idx].attach(SERVO_PINS[idx]);
          attached[idx] = true;
        }
      }
    }
  }
}
