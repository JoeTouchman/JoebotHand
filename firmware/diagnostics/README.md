# Diagnostic sketches

Small standalone sketches written while bringing up the ESP32-S3 and chasing
down a servo bug. None of them are needed to run the hand; they're kept as a
record of how the problem was found.

| Sketch | What it does |
| --- | --- |
| `esp32_test` | Board bring-up: boots, prints PSRAM and flash size, blinks the onboard RGB LED. |
| `servo_test` | Sweeps a single servo on GPIO 4 to check power and wiring. |
| `pin_sweep_test` | Attaches one servo at a time on each soldered finger pin, to map pins to physical fingers. |
| `channel_report` | Attaches all five servos and reports which PWM hardware each pin is *really* routed to. |

## The bug they found

The first ESP32 port drove the servos with the ESP32Servo library, and with
all five attached the fingers didn't move independently. `pin_sweep_test`
drives one pin at a time, which ruled out the soldering and wiring.

`channel_report` read the ESP32-S3's GPIO matrix directly and showed the cause:
on the S3, ESP32Servo (v3.2.1) used the MCPWM peripheral and routed servos 1, 3
and 5 to one output signal and 2 and 4 to another, while its own `getChannel()`
still reported five distinct channels.

The fix, in [`../esp32_hand`](../esp32_hand), drops the library and drives each
servo directly with the core's LEDC PWM (`ledcAttach(pin, 50, 14)`), one channel
per finger. At boot the firmware prints a channel map and flags any pin or
channel collision, so this class of bug can't come back silently.

`servo_test`, `pin_sweep_test` and `channel_report` still use ESP32Servo on
purpose, since they reproduce the problem.
