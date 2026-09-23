#!/usr/bin/env bash

while true; do
    led=""
    for i in /sys/class/leds/*scrolllock; do
        [ -e "$i/device/uevent" ] || continue
        if readlink -f "$i" 2>/dev/null | grep -qi 'c0f4:07c0'; then
            led="$i/brightness"
            break
        fi
    done
    if [ -n "$led" ]; then
        printf '%s' none > "${led%/brightness}/trigger" 2>/dev/null || true
        if [ "$(cat "$led" 2>/dev/null || echo 1)" = "0" ]; then
            printf '1' > "$led" 2>/dev/null || true
        fi
    fi
    sleep 0.2
done
