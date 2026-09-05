#!/usr/bin/env bash
# Launch the SMS desktop (Quickshell). SMS_BRIDGE_URL overrides the bridge address.
mkdir -p ~/.sms-desktop
cd "$(dirname "$0")" && exec quickshell -p .
