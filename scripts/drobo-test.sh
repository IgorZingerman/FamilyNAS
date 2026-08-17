#!/bin/bash

OUT="$HOME/Desktop/drobo_usb_capture_$(date +%Y%m%d_%H%M%S).log"

echo "Drobo capture started: $(date)" | tee "$OUT"

log stream \
--style syslog \
--level debug \
--predicate '
process == "kernel" AND (
 composedMessage CONTAINS[c] "Drobo" OR
 composedMessage CONTAINS[c] "IOAccessoryManager" OR
 composedMessage CONTAINS[c] "IOPort" OR
 composedMessage CONTAINS[c] "USB" OR
 composedMessage CONTAINS[c] "USB4" OR
 composedMessage CONTAINS[c] "Thunderbolt" OR
 composedMessage CONTAINS[c] "SBX" OR
 composedMessage CONTAINS[c] "disconnect" OR
 composedMessage CONTAINS[c] "reset" OR
 composedMessage CONTAINS[c] "failed" OR
 composedMessage CONTAINS[c] "timeout" OR
 composedMessage CONTAINS[c] "authorization"
)' | tee -a "$OUT"