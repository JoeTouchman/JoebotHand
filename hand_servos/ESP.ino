void setup() {
  Serial.begin(115200);
  delay(2000);
  Serial.println("alive");
  Serial.printf("PSRAM: %d bytes\n", ESP.getPsramSize());
}

void loop() {
  neopixelWrite(48, 20, 0, 0);
  delay(500);
  neopixelWrite(48, 0, 0, 20);
  delay(500);
}