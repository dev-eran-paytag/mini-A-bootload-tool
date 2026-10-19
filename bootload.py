#!/usr/bin/env python3
"""mini-A Bootloader Utility -- SAMD21 programming & diagnostics via Atmel-ICE."""

import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# ----------------------------------------------------------------------------
# Paths / config
# ----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
VENDOR_EDBG = ROOT / "vendor" / "edbg"
BUILD_DIR = ROOT / "build"
EDBG_BIN = BUILD_DIR / "edbg"
EDBG_DIAG_BIN = BUILD_DIR / "edbg-diag"
ASSET_DIR = ROOT / "assets"
BOOTLOADER_BIN = ASSET_DIR / "bootloader-feather_m0-v4.0.0.bin"
BOOTLOADER_URL = ("https://github.com/adafruit/uf2-samdx1/releases/download/"
                   "v4.0.0/bootloader-feather_m0-v4.0.0.bin")
UDEV_RULE_SRC = ROOT / "rules" / "90-atmel-ice-uaccess.rules"
UDEV_RULE_DST = Path("/etc/udev/rules.d/90-atmel-ice-uaccess.rules")
LOG_DIR = ROOT / "log"
LOG_FILE = LOG_DIR / "pcb_log.csv"
VENV = Path.home() / ".venvs" / "mcuprog"
VENV_PY = VENV / "bin" / "python"
VENV_PYMCUPROG = VENV / "bin" / "pymcuprog"

TARGET = "samd21"
DEVICE_NAME = "atsamd21g18a"
DEVICE_NAME_HUMAN = "ATSAMD21G18A"
EXPECTED_DID = 0x10010305
VOLTAGE_OK_LO, VOLTAGE_OK_HI = 3.0, 3.6
VOLTAGE_WARN_LO, VOLTAGE_WARN_HI = 0.5, 5.5

# edbg defaults to a 16 MHz SWD clock. Microchip's own spec for SAM parts
# (onlinedocs.microchip.com, "Debug Options": debug sessions over SWD can be
# clocked at up to 10x the CPU clock, but 2 MHz max, full stop) puts that 8x
# over the ceiling -- and right after reset, before firmware has configured
# the clock tree, the SAMD21 core itself is only running at ~1 MHz (internal
# 8 MHz osc / 8 default prescaler), tightening the real limit further via
# the 10x-of-core-clock half of the rule. pymcuprog (Microchip's own tool)
# never exceeds 2 MHz for this reason. So this ladder now stays within
# spec throughout -- it was previously starting 8x over it, which is a
# plausible root cause for edbg's connect failures on hardware Microchip's
# own tools connect to fine.
CLOCK_LADDER_KHZ = [2000, 1000, 400, 125]
RETRYABLE_ERROR_CODES = (0x07, 0x08)  # VERIFY FAILED, COMMUNICATION ERROR

APP_TITLE = "MINI-A BOOTLOADER UTILITY"
APP_VERSION = "v1.0"

# ----------------------------------------------------------------------------
# Terminal styling (retro: box-drawing chars, ANSI colors, no external deps)
# ----------------------------------------------------------------------------

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

def c(code, s):
    return f"\033[{code}m{s}\033[0m" if COLOR else s

def cyan(s):   return c("36", s)
def bcyan(s):  return c("1;36", s)
def green(s):  return c("32", s)
def yellow(s): return c("33", s)
def red(s):    return c("1;31", s)
def bwhite(s): return c("1;37", s)
def dim(s):    return c("2", s)

WIDTH = 64

def hline(char="─"):
    return char * WIDTH

def box(lines, double=False):
    tl, tr, bl, br, h, v = ("╔", "╗", "╚", "╝", "═", "║") if double else ("┌", "┐", "└", "┘", "─", "│")
    print(cyan(tl + h * (WIDTH - 2) + tr))
    for line in lines:
        pad = WIDTH - 4 - len(_strip_ansi(line))
        pad = max(pad, 0)
        print(cyan(v) + " " + line + " " * pad + " " + cyan(v))
    print(cyan(bl + h * (WIDTH - 2) + br))

def _strip_ansi(s):
    return re.sub(r"\033\[[0-9;]*m", "", s)

def banner():
    box([
        bwhite(f"{APP_TITLE} {APP_VERSION}"),
        dim("SAMD21 bootloader programming & diagnostics"),
        dim(f"Target: {DEVICE_NAME_HUMAN}  |  Tool: Atmel-ICE"),
    ], double=True)
    print()

def ok(msg):   print(f"  [{green('OK')}]   {msg}")
def warn(msg): print(f"  [{yellow('WARN')}] {msg}")
def fail(msg): print(f"  [{red('FAIL')}] {msg}")
def info(msg): print(f"  {dim('-')} {msg}")

def pause(prompt="Press ENTER to continue..."):
    try:
        input(f"\n{dim(prompt)}")
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(130)

def confirm(prompt, default=False):
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(f"{prompt}{suffix}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(130)
    if not ans:
        return default
    return ans.startswith("y")

def error_screen(code, title, detail_lines, hint_lines=None):
    print()
    print(red(hline()))
    print(f" {red(f'ERROR 0x{code:02X}')} : {bwhite(title)}")
    print(red(hline()))
    for line in detail_lines:
        print(f" {line}")
    if hint_lines:
        print()
        for line in hint_lines:
            print(f" {dim('->')} {line}")
    print(red(hline()))

# ----------------------------------------------------------------------------
# Process helpers
# ----------------------------------------------------------------------------

RAW_LOG = LOG_DIR / "raw.log"

def _raw_log(cmd, rc, out, err):
    """Append the full command + output of every subprocess to log/raw.log,
    so a generic 'PROGRAMMER ERROR' can be diagnosed after the fact."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with RAW_LOG.open("a") as f:
            f.write(f"=== {datetime.now().isoformat(timespec='seconds')} rc={rc} "
                    f"{' '.join(map(str, cmd))}\n")
            if out.strip():
                f.write(f"--- stdout\n{out.rstrip()}\n")
            if err.strip():
                f.write(f"--- stderr\n{err.rstrip()}\n")
    except OSError:
        pass

def run(cmd, timeout=60):
    """Run a command, return (returncode, stdout, stderr) as text."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        res = (r.returncode, r.stdout, r.stderr)
    except FileNotFoundError:
        res = (127, "", f"command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        res = (124, "", "operation timed out")
    _raw_log(cmd, *res)
    return res

def have_header(*candidates):
    return any(Path(p).is_file() for p in candidates)

# ----------------------------------------------------------------------------
# edbg / pymcuprog error translation -> Microchip-style diagnostics
# ----------------------------------------------------------------------------

EDBG_ERROR_PATTERNS = [
    (r"no debuggers found", 0x01, "DEBUG TOOL NOT FOUND",
     ["No Atmel-ICE was detected on the USB bus."],
     ["Connect the Atmel-ICE via USB and press ENTER to retry."]),
    (r"unknown target device \(DSU_DID = (0x[0-9a-fA-F]+)\)", 0x05, "DEVICE SIGNATURE MISMATCH",
     [], ["Verify SWD connections (SWCLK/SWDIO/GND) and target power.",
          "Check that the correct board is wired to the debugger."]),
    (r"target is locked", 0x06, "DEVICE LOCKED",
     ["The target's security bit is set; programming requires an unlock,",
      "which forces a full chip erase."],
     []),
    (r"verification failed", 0x07, "VERIFY FAILED",
     ["Flash contents did not match after programming."],
     ["Check cable seating and retry -- repeated failures may indicate a damaged chip."]),
    (r"more than one debugger found", 0x09, "MULTIPLE DEBUG TOOLS FOUND",
     ["More than one Atmel-ICE is attached to this host."],
     ["Disconnect all but one debug tool and retry."]),
]

TRANSFER_ERROR_RE = re.compile(
    r"invalid response during transfer \(count = (\d+)/(\d+), status = (\d+)\)")

def translate_edbg_error(stderr_text):
    # "invalid response during transfer" covers two different situations that
    # need different advice: zero transactions acknowledged means the SWD
    # lines never got a response at all (wiring, not speed); a partial count
    # means the link was working and then dropped -- a speed/signal-integrity
    # symptom that the clock-ladder retry can actually fix.
    m = TRANSFER_ERROR_RE.search(stderr_text)
    if m:
        got, total, status = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if got == 0:
            return (0x0C, "TARGET NOT RESPONDING (NO SWD ACK)",
                    [f"No valid response on the SWD lines "
                     f"(0 of {total} transaction{'s' if total != 1 else ''} acknowledged), "
                     f"even after an automatic double-tap reset retry."],
                    ["This is a reset-timing / signal-integrity symptom, not necessarily "
                     "a wiring fault -- it can happen on a blank chip too.",
                     "Check SWDIO, SWCLK, GND and RESET continuity and that the ICE "
                     "cable/pogo pins are fully seated, then retry.",
                     "If it persists on multiple boards, probe SWDIO/SWCLK with a scope "
                     "right after reset release to see what's actually happening."])
        return (0x08, "COMMUNICATION ERROR",
                [f"Debug tool lost communication mid-transfer "
                 f"({got} of {total} transactions completed)."],
                ["Check SWD cable seating and target power stability."])

    for pattern, code, title, detail, hint in EDBG_ERROR_PATTERNS:
        m = re.search(pattern, stderr_text)
        if m:
            d = list(detail)
            if m.groups():
                d.append(f"Reported device ID: {m.group(1)}  (expected 0x{EXPECTED_DID:08X})")
            return code, title, d, hint
    msg = stderr_text.strip().splitlines()[-1] if stderr_text.strip() else "unknown error"
    return 0x0F, "PROGRAMMER ERROR", [msg], ["Re-seat the debugger and target, then retry."]

def run_edbg(binary, args, clock_khz=None, timeout=60):
    cmd = [str(binary), "-t", TARGET, "-b"]
    if clock_khz is not None:
        cmd += ["-c", str(clock_khz)]
    cmd += args
    return run(cmd, timeout=timeout)

def double_tap_reset():
    """Two quick pulses on the debugger's own reset line, timed to land
    inside the SAM-BA bootloader's ~500ms double-tap window (see
    check_start_application() in adafruit/uf2-samdx1 src/main.c) so the
    board stays resident in the bootloader instead of jumping to the
    application. Each '-x' pulse is a standalone edbg invocation (~75ms
    measured) that only drives the reset pin -- it doesn't try to talk to
    the target, so it works even while the target isn't responding on SWD
    at all. Also functions as a generic "give the reset line more settle
    time" retry for connect-timing races that show up on blank chips too."""
    run([str(EDBG_BIN), "-t", TARGET, "-x", "50"], timeout=5)
    time.sleep(0.15)
    run([str(EDBG_BIN), "-t", TARGET, "-x", "50"], timeout=5)
    time.sleep(0.15)

def run_edbg_retry(binary, args, timeout=60):
    """Run edbg, with two layers of automatic recovery before giving up:
    - on "no SWD ACK at all" (code 0x0C), try one debugger-driven
      double-tap reset and retry once at the same clock -- this is the
      first troubleshooting step, since a clock change alone doesn't fix
      a zero-response connect.
    - on a partial/mid-transfer failure (verify/communication error),
      step down through CLOCK_LADDER_KHZ, which *is* a speed/signal-
      integrity symptom.
    Returns (rc, out, err, clock_khz_used)."""
    rc = out = err = None
    double_tapped = False
    i = 0
    while i < len(CLOCK_LADDER_KHZ):
        khz = CLOCK_LADDER_KHZ[i]
        rc, out, err = run_edbg(binary, args, clock_khz=khz, timeout=timeout)
        if rc == 0:
            if double_tapped:
                ok(f"Succeeded at {khz} kHz after a double-tap reset")
            elif i > 0:
                ok(f"Succeeded at {khz} kHz after stepping down from "
                   f"{CLOCK_LADDER_KHZ[0] // 1000} MHz")
            return rc, out, err, khz

        code, _, _, _ = translate_edbg_error(err)

        if code == 0x0C and not double_tapped:
            warn("No response on SWD -- trying a debugger-driven double-tap "
                 "reset as a first troubleshoot ...")
            double_tap_reset()
            double_tapped = True
            continue  # retry at the same clock step, don't advance the ladder

        if code not in RETRYABLE_ERROR_CODES:
            return rc, out, err, khz

        i += 1
        if i < len(CLOCK_LADDER_KHZ):
            warn(f"Transfer error at {khz} kHz -- retrying at "
                 f"{CLOCK_LADDER_KHZ[i]} kHz ...")
    return rc, out, err, CLOCK_LADDER_KHZ[-1]

# ----------------------------------------------------------------------------
# pymcuprog -- Microchip's own connect/program implementation.
#
# edbg is a third-party reimplementation of CMSIS-DAP; on this hardware it
# has shown unreliable connects (see ERROR 0x0C) that MPLAB X / Atmel
# Studio's own Windows driver stack does not exhibit. pymcuprog is Microchip's
# own Python tool (same debug-probe protocol family Microchip's own software
# uses) and has connected reliably here all session for ping/getvoltage, so
# actual programming (erase/write/verify/read) goes through it instead of
# edbg. edbg-diag is kept only for the unique-serial-number read, which
# pymcuprog's device model doesn't expose for this part.
# ----------------------------------------------------------------------------

# pymcuprog logs everything (including errors) through its own logger to
# stdout -- pymcuprog_main.py's exception handlers print the exception's
# *message* directly (`print(error)` / `logger.error("%s", exc)`), never
# the class name, and only truly unexpected exceptions get an "Operation
# failed with <ClassName>: ..." wrapper. So patterns match actual observed
# message text, not exception class names, and run_pymcuprog() returns
# stdout+stderr already combined so callers don't have to guess which
# stream a given message landed on.
PYMCUPROG_ERROR_PATTERNS = [
    (r"Unable to connect to USB device", 0x01, "DEBUG TOOL NOT FOUND",
     ["pymcuprog could not connect to the Atmel-ICE."],
     ["Connect the Atmel-ICE via USB and press ENTER to retry."]),
    (r"Region is locked", 0x06, "DEVICE LOCKED",
     ["The target's security bit is set; programming requires an unlock,",
      "which forces a full chip erase."],
     []),
    (r"is too low \(absolute minimum", 0x03, "TARGET VOLTAGE NOT DETECTED",
     ["pymcuprog reported the target voltage is below the chip's minimum."],
     ["Check target power supply and Atmel-ICE cable seating."]),
    (r"Timeout erasing device via DSU", 0x0D, "ERASE FAILED",
     ["Chip erase did not complete successfully."],
     ["Check cable seating and target power stability, then retry."]),
    (r"PyedbglibError: Read reg failed", 0x0C, "TARGET NOT RESPONDING (NO SWD ACK)",
     ["pymcuprog's register read failed -- the same 'zero ACK' signature "
      "edbg hits when the SWD connect race is lost (see project notes)."],
     ["This is a real, if intermittent, SAMD-family connect race, not a "
      "wiring fault -- the same board can pass a moment later. Retry."]),
]

def translate_pymcuprog_error(text):
    for pattern, code, title, detail, hint in PYMCUPROG_ERROR_PATTERNS:
        if re.search(pattern, text):
            return code, title, list(detail), list(hint)
    # pymcuprog_main.py prints "Done." unconditionally at the very end of
    # every run, success or failure -- it is NEVER the real error message.
    # Prefer the last logged "- ERROR -" line; only fall back to the last
    # non-"Done." line if nothing was logged as an error.
    lines = [l for l in text.strip().splitlines() if l.strip()]
    error_lines = [l for l in lines if " - ERROR - " in l or l.strip().startswith("Error:")]
    if error_lines:
        msg = error_lines[-1]
    else:
        non_done = [l for l in lines if l.strip() != "Done."]
        msg = non_done[-1] if non_done else (lines[-1] if lines else "unknown error")
    return 0x0F, "PROGRAMMER ERROR", [msg], ["Re-seat the debugger and target, then retry."]

# Confirmed via live USB capture (usbmon) to be a real, intermittent SAMD
# connect-time race -- not a wiring fault, not something a fixed number of
# retries is guaranteed to beat, but retrying genuinely does help since the
# same board passes cleanly moments later. 0x0F (unclassified) is included
# because an unrecognized message from a failed connect attempt is more
# likely to be another flavor of the same race than something new.
PYMCUPROG_RETRYABLE_CODES = (0x01, 0x0C, 0x0F)

def run_pymcuprog(args, timeout=60, _attempt=1, _max_attempts=3):
    """Each pymcuprog invocation is a fresh USB HID connect/disconnect."""
    cmd = [str(VENV_PYMCUPROG), "-t", "atmelice", "-d", DEVICE_NAME] + args
    rc, out, err = run(cmd, timeout=timeout)
    text = out + err
    if rc != 0 and _attempt < _max_attempts:
        code, title, _, _ = translate_pymcuprog_error(text)
        if code in PYMCUPROG_RETRYABLE_CODES:
            warn(f"{title} (attempt {_attempt}/{_max_attempts}) -- retrying ...")
            time.sleep(0.3)
            return run_pymcuprog(args, timeout=timeout,
                                  _attempt=_attempt + 1, _max_attempts=_max_attempts)
    return rc, text

def pymcuprog_flash_bootloader():
    """Erase + write + verify the bootloader image via pymcuprog. Returns
    (ok, text) -- text is the combined output of whichever step ran last."""
    rc, text = run_pymcuprog(["erase"], timeout=30)
    if rc != 0:
        return False, text
    rc, text = run_pymcuprog(["write", "-m", "flash", "-o", "0",
                               "-f", str(BOOTLOADER_BIN), "--verify"], timeout=30)
    return rc == 0, text

def pymcuprog_read_flash(offset, size, outfile):
    return run_pymcuprog(["read", "-m", "flash", "-o", str(offset), "-b", str(size),
                           "-f", str(outfile)], timeout=30)

def pymcuprog_identify():
    """Ping the target and decode the DID into (rev, did) via the same
    masks edbg uses (DEVICE_ID_MASK=0xfffff0ff, DEVICE_REV_SHIFT=8,
    DEVICE_REV_MASK=0xf). Returns (ok, rev_or_None, did_or_None, text)."""
    rc, text = run_pymcuprog(["ping"], timeout=20)
    m = re.search(r"Ping response:\s*([0-9a-fA-F]+)", text)
    if rc != 0 or not m:
        return False, None, None, text
    did = int(m.group(1), 16)
    rev = (did >> 8) & 0xF
    return True, chr(ord('A') + rev), did, text

# ----------------------------------------------------------------------------
# Environment / setup checks
# ----------------------------------------------------------------------------

def check_build_tools():
    missing = [t for t in ("gcc", "make") if shutil.which(t) is None]
    if missing:
        fail(f"Build tools missing: {', '.join(missing)}")
        info("Fix:  sudo apt install -y build-essential")
        return False
    ok("Build tools present (gcc, make)")
    return True

def check_headers():
    have_usb = have_header("/usr/include/libusb-1.0/libusb.h")
    have_udev = have_header("/usr/include/libudev.h")
    problems = []
    if not have_usb:
        problems.append("libusb-1.0-0-dev")
    if not have_udev:
        problems.append("libudev-dev")
    if problems:
        fail(f"Missing development headers: {', '.join(problems)}")
        info(f"Fix:  sudo apt install -y {' '.join(problems)}")
        return False
    ok("Development headers present (libusb-1.0, libudev)")
    return True

def check_python_venv():
    if not VENV_PY.exists():
        warn(f"Python venv not found at {VENV}")
        if confirm("Create it now and install pymcuprog + pyusb?", default=True):
            info(f"Creating venv at {VENV} ...")
            rc, _, err = run([sys.executable, "-m", "venv", str(VENV)], timeout=60)
            if rc != 0:
                fail(f"Could not create venv: {err.strip()}")
                return False
        else:
            return False
    rc, out, err = run([str(VENV_PY), "-c", "import usb, pymcuprog"], timeout=15)
    if rc != 0:
        warn("pymcuprog / pyusb not installed in venv")
        if confirm("Install them now (pip, no sudo needed)?", default=True):
            info("Installing pymcuprog + pyusb ...")
            rc, out, err = run([str(VENV) + "/bin/pip", "install", "-q",
                                 "--upgrade", "pip", "pymcuprog", "pyusb"], timeout=120)
            if rc != 0:
                fail(f"pip install failed: {err.strip()[-300:]}")
                return False
        else:
            return False
    ok("pymcuprog + pyusb available (~/.venvs/mcuprog)")
    return True

def check_edbg_binaries():
    need_build = not EDBG_BIN.exists() or not EDBG_DIAG_BIN.exists()
    if need_build:
        warn("edbg not built yet")
        info("Building edbg from vendored source (vendor/edbg) ...")
        BUILD_DIR.mkdir(exist_ok=True)
        rc, out, err = run(["make", "-C", str(VENDOR_EDBG), "clean", "all", "edbg-diag"], timeout=60)
        if rc != 0:
            fail("Build failed:")
            for line in err.strip().splitlines()[-8:]:
                info(line)
            return False
        shutil.copy2(VENDOR_EDBG / "edbg", EDBG_BIN)
        shutil.copy2(VENDOR_EDBG / "edbg-diag", EDBG_DIAG_BIN)
        (VENDOR_EDBG / "edbg").unlink(missing_ok=True)
        (VENDOR_EDBG / "edbg-diag").unlink(missing_ok=True)
    ok("edbg tool built (build/edbg, build/edbg-diag)")
    return True

def check_bootloader_asset():
    if BOOTLOADER_BIN.exists() and BOOTLOADER_BIN.stat().st_size > 0:
        ok(f"Bootloader image present ({BOOTLOADER_BIN.name})")
        return True
    warn("Bootloader image missing")
    if not confirm(f"Download it now from adafruit/uf2-samdx1 (v4.0.0)?", default=True):
        return False
    try:
        info("Downloading ...")
        ASSET_DIR.mkdir(exist_ok=True)
        urllib.request.urlretrieve(BOOTLOADER_URL, BOOTLOADER_BIN)
    except Exception as e:
        fail(f"Download failed: {e}")
        return False
    ok(f"Bootloader image downloaded ({BOOTLOADER_BIN.name})")
    return True

def check_usb_permission():
    """Best-effort: if an Atmel-ICE is already plugged in, verify the raw
    USB node is actually accessible (uaccess), rather than trusting any
    specific udev rule file to be present."""
    rc, out, _ = run(["lsusb", "-d", "03eb:"], timeout=5)
    if rc != 0 or not out.strip():
        info("Atmel-ICE not currently connected (will check again before each operation)")
        return True
    m = re.search(r"Bus (\d+) Device (\d+)", out)
    if not m:
        return True
    node = f"/dev/bus/usb/{int(m.group(1)):03d}/{int(m.group(2)):03d}"
    if os.access(node, os.R_OK | os.W_OK):
        ok(f"Atmel-ICE detected and accessible ({node})")
        return True
    fail(f"Atmel-ICE detected but not accessible ({node})")
    info("Fix:  sudo cp rules/90-atmel-ice-uaccess.rules /etc/udev/rules.d/")
    info("      sudo udevadm control --reload-rules && sudo udevadm trigger")
    info("      then unplug/replug the debugger")
    return False

def run_setup_checks():
    box([bwhite("VERIFYING INSTALLATION")], double=False)
    while True:
        results = [
            check_build_tools(),
            check_headers(),
            check_python_venv(),
            check_edbg_binaries(),
            check_bootloader_asset(),
            check_usb_permission(),
        ]
        if all(results):
            print()
            ok("All checks passed.")
            return True
        print()
        if not confirm("Some checks failed. Re-check now?", default=True):
            return False
        print()

# ----------------------------------------------------------------------------
# Atmel-ICE presence
# ----------------------------------------------------------------------------

def atmel_ice_present():
    rc, out, _ = run(["lsusb", "-d", "03eb:"], timeout=5)
    return rc == 0 and bool(out.strip())

def wait_for_atmel_ice(prompt="Connect the Atmel-ICE to the target PCB and press ENTER"):
    while True:
        pause(prompt)
        if atmel_ice_present():
            return True
        error_screen(0x01, "DEBUG TOOL NOT FOUND",
                     ["No Atmel-ICE was detected on the USB bus."],
                     ["Check the USB cable and connection, then press ENTER to retry.",
                      "Type Q + ENTER to return to the menu."])
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(130)
        if ans == "q":
            return False

# ----------------------------------------------------------------------------
# Core operations
# ----------------------------------------------------------------------------

def read_voltage():
    """Returns (ok, voltage_or_None, message)."""
    rc, out, err = run([str(VENV_PYMCUPROG), "getvoltage", "-t", "atmelice",
                         "-d", DEVICE_NAME], timeout=20)
    m = re.search(r"Measured voltage:\s*([\d.]+)\s*V", out)
    if rc != 0 or not m:
        return False, None, (err or out).strip().splitlines()[-1] if (err or out).strip() else "no response"
    return True, float(m.group(1)), None

def voltage_gate():
    """Measure target voltage and gate on it. Returns True to proceed."""
    okv, voltage, msg = read_voltage()
    if not okv:
        error_screen(0x02, "VOLTAGE READ FAILED",
                     [f"Could not measure target voltage: {msg}"],
                     ["Check debugger connection and target power."])
        return False
    if voltage < VOLTAGE_WARN_LO:
        error_screen(0x03, "TARGET VOLTAGE NOT DETECTED",
                     [f"Measured voltage: {voltage:.2f} V"],
                     ["Check target power supply and Atmel-ICE cable seating.",
                      "The board may be unpowered or disconnected."])
        return False
    if voltage > VOLTAGE_WARN_HI:
        error_screen(0x04, "TARGET VOLTAGE OUT OF RANGE",
                     [f"Measured voltage: {voltage:.2f} V  (expected "
                      f"{VOLTAGE_OK_LO:.1f}-{VOLTAGE_OK_HI:.1f} V)",
                      "This exceeds safe operating range for the target."],
                     [])
        return False
    if voltage < VOLTAGE_OK_LO or voltage > VOLTAGE_OK_HI:
        warn(f"Target voltage {voltage:.2f} V is outside nominal "
             f"{VOLTAGE_OK_LO:.1f}-{VOLTAGE_OK_HI:.1f} V range")
        if not confirm("Continue anyway?", default=False):
            return False
    else:
        ok(f"Target voltage: {voltage:.2f} V")
    return True

def do_flash_bootloader():
    print()
    box([bwhite("LOAD NEW PCB -- FLASH BOOTLOADER")])
    if not wait_for_atmel_ice():
        return
    if not voltage_gate():
        pause()
        return

    okid, rev, did, ierr = pymcuprog_identify()
    if not okid:
        code, title, detail, hint = translate_pymcuprog_error(ierr or "")
        error_screen(code, title, detail, hint)
        log_row("flash", result="FAIL", notes=title)
        pause()
        return
    if did != EXPECTED_DID:
        error_screen(0x05, "DEVICE SIGNATURE MISMATCH",
                     [f"Reported device ID: 0x{did:08X}  (expected 0x{EXPECTED_DID:08X})"],
                     ["Verify the correct board is wired to the debugger."])
        log_row("flash", result="FAIL", notes="signature mismatch")
        pause()
        return
    ok(f"Target identified: {DEVICE_NAME_HUMAN} (Rev {rev})")

    print()
    info("Erasing ...")
    okf, text = pymcuprog_flash_bootloader()
    if not okf:
        code, title, detail, hint = translate_pymcuprog_error(text)
        error_screen(code, title, detail, hint)
        log_row("flash", result="FAIL", notes=title)
        pause()
        return

    ok("Erase ... done")
    ok("Program ... done")
    ok("Verify ... done")
    print()
    print(green("  BOOTLOADER LOADED SUCCESSFULLY"))

    # auto-recollect for the log
    serial, rev, voltage = collect_chip_data(quiet=True)
    log_row("flash", serial=serial, voltage=voltage, rev=rev, result="OK")
    pause()

def collect_chip_data(quiet=False):
    """Read voltage + serial number. Returns (serial, rev, voltage) -- any may be None."""
    okv, voltage, vmsg = read_voltage()
    if not quiet:
        if okv:
            ok(f"Target voltage: {voltage:.2f} V")
        else:
            fail(f"Voltage read failed: {vmsg}")

    okid, rev, did, ierr = pymcuprog_identify()
    if not okid:
        if not quiet:
            code, title, detail, hint = translate_pymcuprog_error(ierr or "")
            error_screen(code, title, detail, hint)
        return None, None, voltage

    # unique serial number lives outside pymcuprog's device memory map for
    # this part, so it still goes through edbg-diag's raw NVM read
    rc, out, err, khz = run_edbg_retry(EDBG_DIAG_BIN,
                                        ["-r", "-o", "0xDEAD00", "-z", "256", "-f", "/dev/null"],
                                        timeout=30)
    ser_m = re.search(r"SERIAL:\s*([0-9A-F]{32})", out)
    serial = ser_m.group(1) if ser_m else None

    if not quiet:
        ok(f"Device revision: Rev {rev}")
        if serial:
            ok(f"Serial number:   {serial}")
        else:
            code, title, detail, hint = translate_edbg_error(err)
            warn(f"Serial number read failed ({title})")
    return serial, rev, voltage

def do_recollect():
    print()
    box([bwhite("RECOLLECT CHIP DATA")])
    if not wait_for_atmel_ice():
        return
    print()
    serial, rev, voltage = collect_chip_data()
    log_row("recollect", serial=serial, voltage=voltage, rev=rev,
            result="OK" if serial else "FAIL")
    pause()

def do_check_bootloader_info():
    print()
    box([bwhite("CHECK BOOTLOADER INFO")])
    if not wait_for_atmel_ice():
        return
    if not voltage_gate():
        pause()
        return

    okid, rev, did, ierr = pymcuprog_identify()
    if not okid:
        code, title, detail, hint = translate_pymcuprog_error(ierr or "")
        error_screen(code, title, detail, hint)
        pause()
        return
    if did != EXPECTED_DID:
        error_screen(0x05, "DEVICE SIGNATURE MISMATCH",
                     [f"Reported device ID: 0x{did:08X}  (expected 0x{EXPECTED_DID:08X})"],
                     ["Verify the correct board is wired to the debugger."])
        pause()
        return
    ok(f"Target identified: {DEVICE_NAME_HUMAN} (Rev {rev})")

    print()
    info("Reading flash (bootloader region) ...")
    size = BOOTLOADER_BIN.stat().st_size
    tmp = BUILD_DIR / "readback.bin"
    rc, text = pymcuprog_read_flash(0, size, tmp)
    if rc != 0:
        code, title, detail, hint = translate_pymcuprog_error(text)
        error_screen(code, title, detail, hint)
        pause()
        return

    ref_hash = hashlib.sha256(BOOTLOADER_BIN.read_bytes()).hexdigest()
    got_hash = hashlib.sha256(tmp.read_bytes()[:size]).hexdigest()
    tmp.unlink(missing_ok=True)

    print()
    if got_hash == ref_hash:
        print(green(f"  BOOTLOADER OK  ({BOOTLOADER_BIN.name}, sha256 match)"))
    else:
        if got_hash == hashlib.sha256(b"\xff" * size).hexdigest():
            error_screen(0x0A, "NO BOOTLOADER PRESENT",
                         ["Flash region is blank (erased / 0xFF)."],
                         ["Use 'Load new PCB' to flash the bootloader."])
        else:
            error_screen(0x0B, "BOOTLOADER MISMATCH",
                         ["Flash contents do not match the reference bootloader image.",
                          f"Expected sha256: {ref_hash[:16]}...",
                          f"Found sha256:    {got_hash[:16]}..."],
                         ["This may be an older/different bootloader version, or a "
                          "partially-programmed chip -- re-flash if unsure."])
    pause()

def view_log():
    print()
    box([bwhite("SESSION LOG")])
    if not LOG_FILE.exists():
        info("No log entries yet.")
        pause()
        return
    with LOG_FILE.open() as f:
        rows = list(csv.reader(f))
    if len(rows) <= 1:
        info("No log entries yet.")
        pause()
        return
    header, entries = rows[0], rows[1:]
    for row in entries[-15:]:
        d = dict(zip(header, row))
        result_c = green(d["result"]) if d["result"] == "OK" else red(d["result"])
        print(f"  {d['timestamp']}  {d['action']:<10} {result_c:<12} "
              f"serial={d.get('serial') or '-':<10} volt={d.get('voltage') or '-'}")
    if len(entries) > 15:
        info(f"... {len(entries) - 15} earlier entries in {LOG_FILE}")
    pause()

def log_row(action, serial=None, voltage=None, rev=None, result="OK", notes=""):
    LOG_DIR.mkdir(exist_ok=True)
    new = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "action", "result", "serial", "voltage", "rev", "notes"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), action, result,
                    serial or "", f"{voltage:.2f}" if voltage is not None else "", rev or "", notes])

# ----------------------------------------------------------------------------
# Menu
# ----------------------------------------------------------------------------

MENU = [
    ("1", "Load new PCB (flash bootloader)", do_flash_bootloader),
    ("2", "Check bootloader info", do_check_bootloader_info),
    ("3", "Recollect chip data (voltage / serial)", do_recollect),
    ("4", "View session log", view_log),
]

def main_menu():
    while True:
        os.system("clear" if os.name == "posix" else "cls")
        banner()
        box([
            "",
            *[f" [{bwhite(k)}] {label}" for k, label, _ in MENU],
            f" [{bwhite('Q')}] Quit",
            "",
        ])
        try:
            choice = input(f"\n{bwhite('Select option:')} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if choice == "q":
            break
        for key, _, fn in MENU:
            if choice == key:
                fn()
                break
        else:
            if choice:
                warn(f"Unrecognized option '{choice}'")
                time.sleep(1)

# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main():
    os.system("clear" if os.name == "posix" else "cls")
    banner()
    if not run_setup_checks():
        print()
        fail("Setup incomplete. Resolve the items above and re-run this tool.")
        sys.exit(1)

    print()
    box([bwhite("READY")])
    do_flash_bootloader()
    main_menu()
    print(dim("\nGoodbye.\n"))

if __name__ == "__main__":
    main()
