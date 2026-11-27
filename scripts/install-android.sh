#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APK_PATH="$ROOT_DIR/android-cam/app/build/outputs/apk/debug/app-debug.apk"

if [[ ! -f "$APK_PATH" ]]; then
  echo "APK not built yet — building android-cam..."
  (cd "$ROOT_DIR/android-cam" && ./gradlew assembleDebug)
fi

DEVICES=$(adb devices | awk 'NR>1 && $2=="device" {print $1}')
if [[ -z "$DEVICES" ]]; then
  echo "No authorized Android devices found via adb."
  echo "Plug in a phone with USB debugging enabled and accept the prompt on-screen."
  exit 1
fi

for SERIAL in $DEVICES; do
  MODEL=$(adb -s "$SERIAL" shell getprop ro.product.model | tr -d '\r')
  echo "Installing LEDMap Cam onto $MODEL ($SERIAL)..."
  adb -s "$SERIAL" install -r -g "$APK_PATH"
  echo "Launching LEDMap Cam on $MODEL..."
  adb -s "$SERIAL" shell am start -n com.ledmapping.cam/.MainActivity >/dev/null
  echo "Done with $MODEL ($SERIAL)! You can unplug it now and connect it to the shared Wi-Fi."
done
