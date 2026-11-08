# GPIO-controlled line-laser driver

## Verified assembly

The bench driver was completed and hardware-verified on 2026-08-18. It switches
the existing 5 V `CHT1230` line-laser module with a discrete `2N2222` NPN
transistor while retaining the manual switch as an independent positive-rail
disconnect.

```text
GOOUUU 5V --- manual switch --- laser positive
                                   laser negative --- 2N2222 collector

GOOUUU G14 / GPIO14 --- 1 kOhm ---+--- 2N2222 base
                                  |
                                10 kOhm
                                  |
GOOUUU GND -----------------------+--- 2N2222 emitter
```

The common-GND connection is mandatory. Laser negative connects only to the
collector; it must not also be tied directly to GND.

For the exact installed transistor, the supplier's product description and the
working assembly agree on this positional pinout with the flat marked face
towards the operator and the leads pointing down:

```text
left = emitter, middle = base, right = collector
```

The control connection is the board header hole silk-screened `G14`: the second
hole from the bottom of the left header, immediately above `5V`. GPIO 14 is not
used by the verified camera pin map, is not a boot-strapping pin and is not the
native USB pair. Firmware defines it as `kLaserPin = 14`.

## Electrical measurements and component margins

The module current was measured in series at **13.0 mA** from approximately 5 V,
or about 65 mW electrical input. The GPIO drives the base only:

- nominal base resistor: 1 kOhm;
- base-emitter pull-down: 10 kOhm;
- approximate current through the 1 kOhm resistor at 3.3 V: 2.6 mA;
- approximate 1 kOhm resistor dissipation: 6.8 mW;
- approximate transistor dissipation at 0.1--0.3 V saturation voltage: 1.3--3.9 mW.

Those values are far below ordinary resistor and `2N2222` limits. No heat sink is
required. A flyback diode is not applicable because the laser module is not an
inductive load. No extra series resistor belongs in the 5 V laser path because
this is the same complete 5 V module that was tested directly, not a bare laser
diode.

No additional capacitor was required for the verified switching test. If later
captures show supply-correlated image noise, USB dropouts or ESP32 resets, add a
known 100 nF ceramic capacitor and, if needed, a correctly polarized 10--47 uF
electrolytic capacitor between 5 V and GND near the driver. Do not place a
capacitor from GPIO to base as a substitute for proper control logic.

## Executed validation

The following sequence has been executed on the real assembly:

1. With the control wire open, the manual switch was enabled and the laser stayed
   off; the 10 kOhm pull-down therefore held the transistor off.
2. Applying 3.3 V to the control wire through the installed 1 kOhm resistor
   switched the laser on; removing it switched the laser off.
3. The board was positively identified on `/dev/ttyUSB0` through its CH340C
   interface before upload.
4. The current repository firmware built successfully and was uploaded at
   921600 baud. Esptool identified ESP32-S3 revision 0.2 with 8 MiB embedded
   PSRAM and verified every written-region hash before reset.
5. The localhost viewer acknowledged `LASER OFF pin=14`, `LASER ON pin=14` and
   final `LASER OFF pin=14` responses from the firmware.
6. With the manual switch enabled, a three-second software pulse visibly lit the
   physical laser. The operator confirmed the light, and the final command
   returned it to off.
7. VGA JPEG capture continued at approximately 5 FPS during the switching test
   without a camera or serial error.

This verifies the complete path:

```text
localhost viewer -> USB-UART -> firmware -> GPIO14 -> 1 kOhm -> 2N2222 -> laser
```

The board and viewer were left with the reported laser state off. The
fixture was not installed for this electrical/software test, so `R` registered
pair capture with the finished driver remains to be validated after mechanical
installation.

## Operating boundary

The driver itself has ample electrical margin for prolonged switching at the
measured 13 mA, but **no multi-hour thermal soak has been completed** and the
laser module's continuous-duty rating has not been documented from a primary
datasheet. Initial long runs therefore remain supervised and staged.

The firmware drives GPIO14 low early in `setup()`, and the 10 kOhm resistor keeps
the base low while the GPIO is high-impedance during boot or reset. These are
startup protections, not a runtime interlock:

- command `1` keeps the laser on until `0`, reset or power removal;
- the firmware has no maximum-on-time watchdog yet (draft PR #7);
- the live viewer switches the laser off after `--laser-max-on-s` (default
  60 s) and when the browser page stops polling for 5 s, but only while the
  viewer process and the USB link are alive;
- an abrupt host or cable failure does not guarantee an off command;
- a normal viewer shutdown commands the laser off and exits with code 1 and
  `LASER STATE UNKNOWN` if it cannot confirm that;
- the manual switch remains the authoritative physical disconnect.

Do not leave the prototype unattended while the manual switch is enabled. Before
fixture work, verify laser-off at boot, after viewer exit and after USB reconnect.
Use only one USB power source unless the board's power-path isolation has been
separately established.

## Local control

The standard viewer remains bound to localhost only:

```bash
sudo setfacl -m u:"$(id -un)":rw /dev/ttyUSB0
nice -n 15 ionice -c3 .venv/bin/python tools/live_viewer.py \
  --port /dev/ttyUSB0 --baud 921600 --bind 127.0.0.1 \
  --http-port 8765 --fps 5
```

Open `http://127.0.0.1:8765/` and use **Laser on** / **Laser off**. The page asks
for confirmation before switching on and shows a red banner with the remaining
on-time while the laser is lit. The temporary
ACL may need to be reapplied when reconnecting USB because a new device node can
be created.
