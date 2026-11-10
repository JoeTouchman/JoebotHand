// Servo test: the first ESP32-S3 check. Sweeps one servo on GPIO 4 through
// 0 / 90 / 180 degrees with ESP32Servo to confirm power and signal wiring.
// (The main firmware no longer uses ESP32Servo - see ../README.md.)

#include <ESP32Servo.h>

Servo s;

void setup() {
  Serial.begin(115200);
  delay(2000);
  s.setPeriodHertz(50);
  s.attach(4, 500, 2400);
  Serial.println("servo ready");
}

void loop() {
  Serial.println("0");
  s.write(0);
  delay(1500);
  Serial.println("90");
  s.write(90);
  delay(1500);
  Serial.println("180");
  s.write(180);
  delay(1500);
}
