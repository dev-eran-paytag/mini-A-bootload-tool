#!/usr/bin/env bash
# Captures Atmel-ICE USB traffic (bus 1, usbmon) while you run bootload.py,
# so a connect failure gets caught on the wire instead of guessed at.
#
# Order is enforced by the script, not by you watching a clock:
#   1. usbmon capture starts in the background FIRST (long window, 180s)
#   2. you wire up the board and confirm you're ready
#   3. bootload.py launches automatically
#   4. the moment bootload.py exits, the capture stops and is saved
#
# Needs sudo for usbmon (kernel module + debugfs read). bootload.py itself
# runs as your normal user, not root, so venv paths resolve correctly.

set -e

DURATION=180
OUT="/tmp/atmelice_usbmon_$(date +%s).txt"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Loading usbmon (may prompt for your password)..."
sudo modprobe usbmon

echo "Starting background USB capture (bus 1, up to ${DURATION}s) -> $OUT"
sudo bash -c "timeout $DURATION cat /sys/kernel/debug/usb/usbmon/1u > '$OUT'" &
CAPTURE_PID=$!
sleep 1

echo
echo "Capture is running. Wire up the board to the Atmel-ICE now."
read -r -p "Press ENTER when ready to launch bootload.py... "

cd "$PROJECT_DIR"
python3 bootload.py
RC=$?

echo
echo "bootload.py exited (code $RC). Stopping capture..."
sudo pkill -f "cat /sys/kernel/debug/usb/usbmon/1u" >/dev/null 2>&1 || true
wait "$CAPTURE_PID" 2>/dev/null || true
sudo chmod 644 "$OUT"

echo
echo "Capture saved: $OUT ($(wc -l < "$OUT") lines)"
