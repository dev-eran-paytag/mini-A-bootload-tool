# mini-A Bootloader Utility

Terminal tool for flashing the SAM-BA/UF2 bootloader onto blank ATSAMD21G18A
chips via an Atmel-ICE (SWD), and checking chip health (voltage, unique
serial number, bootloader integrity) before/after. Built for the mini-A
Feather-M0-based boards; see `~/projects/feather_m0_mini-A_base` for the
application firmware that gets loaded over USB afterward.

## Run

```
python3 bootload.py
```

First run installs/builds everything it needs (Python venv + pymcuprog,
`edbg` built from `vendor/edbg`) except two things that need `sudo` and
aren't auto-fixable: `build-essential`/`libusb-1.0-0-dev`/`libudev-dev` via
apt, and (rarely needed — see below) the udev rule in `rules/`.

## Layout

| Path | What |
|---|---|
| `bootload.py` | the app |
| `vendor/edbg/` | vendored `edbg` (Atmel-ICE/EDBG SWD tool), patched to add a serial-number read path (`EDBG_DIAG_SERIAL`) |
| `assets/` | reference bootloader image (`bootloader-feather_m0-v4.0.0.bin`, from adafruit/uf2-samdx1) |
| `rules/` | udev rule granting USB access to the Atmel-ICE, for systems where nothing else already does (see note below) |
| `log/pcb_log.csv` | append-only record of every flash/recollect, one row per board |
| `build/` | compiled `edbg` / `edbg-diag` (gitignored, rebuilt automatically) |

Note: on a system with `avrdude` installed, its own udev rules already grant
Atmel-ICE access — the tool checks the actual device node permission at
runtime rather than assuming either rule is what's providing it.
