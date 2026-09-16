#!/usr/bin/env bash
echo 1 | pkexec tee /sys/class/leds/input32::scrolllock/brightness
