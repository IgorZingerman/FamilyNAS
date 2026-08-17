#!/bin/bash

# Drobo N5 Diagnostic Collector
# Safe read-only checks only.
# Does NOT mount, repair, modify, or write to disks.

set -u

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
REPORT="drobo_diagnostic_${TIMESTAMP}.txt"

exec > >(tee -a "$REPORT") 2>&1

echo "=============================================="
echo "Drobo N5 Diagnostic Report"
echo "Generated: $(date)"
echo "=============================================="
echo

echo "## macOS Information"
echo "----------------------------------------------"
sw_vers
echo

echo "## Hardware Information"
echo "----------------------------------------------"
system_profiler SPHardwareDataType
echo

echo "## USB Devices"
echo "----------------------------------------------"
system_profiler SPUSBDataType
echo

echo "## Disk Inventory"
echo "----------------------------------------------"
diskutil list
echo

echo "## External Disk Information"
echo "----------------------------------------------"
diskutil info -all | grep -A25 -B2 -i "external\|drobo\|usb"
echo

echo "## Mounted Volumes"
echo "----------------------------------------------"
mount
echo

echo "## File System Information"
echo "----------------------------------------------"
diskutil apfs list 2>/dev/null || true
echo

echo "## Recent Disk/System Messages"
echo "----------------------------------------------"
log show \
  --predicate 'eventMessage CONTAINS[c] "disk" OR eventMessage CONTAINS[c] "usb" OR eventMessage CONTAINS[c] "drobo"' \
  --last 30m \
  --info 2>/dev/null | tail -200

echo

echo "=============================================="
echo "Diagnostic complete."
echo "Report saved to:"
echo "$(pwd)/$REPORT"
echo "=============================================="