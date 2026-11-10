// Channel report: attaches all five finger servos the same way the main
// sketch does (allocateTimer 0-3, setPeriodHertz(50), attach(pin, 544, 2400)),
// but never writes a position, so no pulses are sent and nothing moves.
// For each pin it prints what PWM hardware it actually got:
//   lib   = what ESP32Servo thinks it allocated (attach() return value;
//           100+ means MCPWM: 100 + unit*10 + timer*2 + (A=0, B=1))
//   core  = what the Arduino core's LEDC layer has bound to the pin
//   route = which peripheral signal the GPIO matrix really drives the pin with

#include <ESP32Servo.h>
#include "esp32-hal-periman.h"
#include "esp32-hal-ledc.h"
#include "soc/gpio_reg.h"
#include "soc/gpio_sig_map.h"

int pins[5] = {4, 5, 6, 7, 15};
int libChannel[5];
Servo servos[5];

int routedSignal(int pin) {
  return REG_READ(GPIO_FUNC0_OUT_SEL_CFG_REG + 4 * pin) & 0x1FF;
}

void printLib(int ch) {
  if (ch >= 100) {
    int k = ch - 100;
    Serial.printf("MCPWM unit %d timer %d out %c", k / 10, (k % 10) / 2, (k % 2) ? 'B' : 'A');
  } else {
    Serial.printf("LEDC channel %d", ch);
  }
}

void printRoute(int sig) {
  if (sig >= LEDC_LS_SIG_OUT0_IDX && sig <= LEDC_LS_SIG_OUT7_IDX) {
    Serial.printf("LEDC channel %d", sig - LEDC_LS_SIG_OUT0_IDX);
  } else if (sig >= PWM0_OUT0A_IDX && sig <= PWM0_OUT2B_IDX) {
    int k = sig - PWM0_OUT0A_IDX;
    Serial.printf("MCPWM unit 0 timer %d out %c", k / 2, (k % 2) ? 'B' : 'A');
  } else if (sig >= PWM1_OUT0A_IDX && sig <= PWM1_OUT2B_IDX) {
    int k = sig - PWM1_OUT0A_IDX;
    Serial.printf("MCPWM unit 1 timer %d out %c", k / 2, (k % 2) ? 'B' : 'A');
  } else if (sig == SIG_GPIO_OUT_IDX) {
    Serial.print("plain GPIO (no PWM)");
  } else {
    Serial.printf("signal %d", sig);
  }
}

void printRow(int i) {
  int pin = pins[i];
  Serial.printf("GPIO %2d | lib: ch %3d = ", pin, libChannel[i]);
  printLib(libChannel[i]);
  ledc_channel_handle_t *bus = (ledc_channel_handle_t *)perimanGetPinBus(pin, ESP32_BUS_TYPE_LEDC);
  if (bus) {
    Serial.printf(" | core: LEDC ch %d timer %d", bus->channel, bus->timer_num);
  } else {
    Serial.print(" | core: no LEDC");
  }
  Serial.print(" | route: ");
  printRoute(routedSignal(pin));
  Serial.println();
}

void printTable() {
  Serial.printf("core %s, IDF %s, ESP32Servo 3.2.1\n", ESP_ARDUINO_VERSION_STR, esp_get_idf_version());
  for (int i = 0; i < 5; i++) printRow(i);
  Serial.println();
}

void setup() {
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  ESP32PWM::allocateTimer(2);
  ESP32PWM::allocateTimer(3);
  Serial.begin(115200);
  delay(2000);
  for (int i = 0; i < 5; i++) {
    servos[i].setPeriodHertz(50);
    libChannel[i] = servos[i].attach(pins[i], 544, 2400);
    Serial.printf("attached GPIO %d -> lib channel %d\n", pins[i], libChannel[i]);
  }
  Serial.println();
}

void loop() {
  printTable();  // reprinted every 5 s so it's easy to catch
  delay(5000);
}
