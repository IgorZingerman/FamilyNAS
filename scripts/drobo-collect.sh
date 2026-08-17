#!/bin/bash

OUT="$HOME/Desktop/drobo_debug_$(date +%Y%m%d_%H%M%S)"
mkdir "$OUT"

echo "Collecting Drobo/macOS USB diagnostics..."
echo "Output: $OUT"

echo "== diskutil list =="
diskutil list > "$OUT/diskutil_list.txt"

echo "== diskutil external info =="
for d in /dev/disk*; do
    diskutil info "$d" 2>/dev/null
done > "$OUT/diskutil_info_all.txt"

echo "== USB tree =="
system_profiler SPUSBDataType > "$OUT/usb_tree.txt"

echo "== Thunderbolt =="
system_profiler SPThunderboltDataType > "$OUT/thunderbolt.txt"

echo "== IORegistry USB devices =="
ioreg -p IOUSB -l -w 0 > "$OUT/ioreg_usb.txt"

echo "== IORegistry matching Drobo =="
ioreg -p IOUSB -l -w 0 | grep -i -A20 -B10 drobo > "$OUT/ioreg_drobo.txt"

echo "== Recent kernel USB/Drobo events =="
log show --last 15m \
--predicate 'process == "kernel" AND (
 composedMessage CONTAINS[c] "Drobo" OR
 composedMessage CONTAINS[c] "USB" OR
 composedMessage CONTAINS[c] "IOAccessoryManager" OR
 composedMessage CONTAINS[c] "IOPort" OR
 composedMessage CONTAINS[c] "SBX" OR
 composedMessage CONTAINS[c] "Thunderbolt" OR
 composedMessage CONTAINS[c] "disconnect" OR
 composedMessage CONTAINS[c] "reset" OR
 composedMessage CONTAINS[c] "error" OR
 composedMessage CONTAINS[c] "failed"
)' > "$OUT/kernel_usb_drobo.log"

echo "== Current mounted volumes =="
mount > "$OUT/mounts.txt"

echo "== APFS containers =="
diskutil apfs list > "$OUT/apfs_list.txt"

echo "== IORegistry external storage =="
ioreg -p IOService -l -w 0 | \
grep -i -A20 -B10 \
"USB\|SCSI\|Mass\|Storage\|Drobo" > "$OUT/ioreg_storage.txt"

echo
echo "DONE"
echo "Zip this folder:"
echo "$OUT"