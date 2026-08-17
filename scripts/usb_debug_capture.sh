#!/bin/bash

OUT="$HOME/Desktop/usb_debug_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"

echo "USB-C diagnostic capture"
echo "Output: $OUT"
echo

echo "[1/5] Collecting system information..."

system_profiler SPUSBDataType > "$OUT/usb_tree.txt"
system_profiler SPThunderboltDataType > "$OUT/thunderbolt.txt" 2>/dev/null
ioreg -p IOUSB -l -w 0 > "$OUT/ioreg_usb.txt"
ioreg -p IOService -n IOUSBHostDevice -l -w 0 > "$OUT/ioreg_usbhost.txt"

uname -a > "$OUT/system.txt"
sw_vers >> "$OUT/system.txt"

echo "[2/5] Capturing USB-related kernel state..."

log show --last 5m \
  --predicate '
  process == "kernel" AND
  (
    eventMessage CONTAINS[c] "USB" OR
    eventMessage CONTAINS[c] "IOAccessoryManager" OR
    eventMessage CONTAINS[c] "IOPort" OR
    eventMessage CONTAINS[c] "ACM" OR
    eventMessage CONTAINS[c] "TRM"
  )' \
  > "$OUT/recent_usb_log.txt"


echo "[3/5] Starting live USB monitor..."
echo "Plug/unplug the problem device now."
echo "Press Ctrl+C when finished."
echo

log stream \
  --style compact \
  --level debug \
  --predicate '
  process == "kernel" AND
  (
    eventMessage CONTAINS[c] "IOAccessoryManager" OR
    eventMessage CONTAINS[c] "IOPort" OR
    eventMessage CONTAINS[c] "USB" OR
    eventMessage CONTAINS[c] "IOUSB" OR
    eventMessage CONTAINS[c] "ACM" OR
    eventMessage CONTAINS[c] "TRM" OR
    eventMessage CONTAINS[c] "disconnect" OR
    eventMessage CONTAINS[c] "failed" OR
    eventMessage CONTAINS[c] "timeout"
  )' | tee "$OUT/live_usb.log"


echo
echo "[4/5] Finished."
echo "Logs saved in:"
echo "$OUT"

echo
echo "[5/5] Quick error scan:"
grep -Ei \
"fail|error|timeout|stall|disconnect|denied|restricted|unauthorized|not ready" \
"$OUT"/*.log "$OUT"/*.txt \
> "$OUT/suspect_lines.txt"

echo "Review:"
echo "  $OUT/suspect_lines.txt"