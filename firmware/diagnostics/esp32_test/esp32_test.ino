// Board bring-up: proves the ESP32-S3 boots, prints its PSRAM and flash
// sizes over USB serial, and blinks the onboard RGB LED (GPIO 48).

void setup() {
  Serial.begin(115200);
  delay(2000);
  Serial.println("alive");
  Serial.printf("PSRAM: %d bytes\n", ESP.getPsramSize());
  Serial.printf("Flash: %d bytes\n", ESP.getFlashChipSize());
}

void loop() {
  neopixelWrite(48, 20, 0, 0);
  delay(500);
  neopixelWrite(48, 0, 0, 20);
  delay(500);
}
