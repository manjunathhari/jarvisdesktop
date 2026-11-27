#!/usr/bin/env bash
# THROWAWAY (slice 0 spike): install the test app in the emulator, open it, and print what
# Android and the app logged, so a start-up failure can be read from the build log.
set -u
apk=$(find app/src-tauri/gen/android -name '*.apk' | head -1)
pkg=dev.jarvis.spike
adb logcat -c
adb install -g "$apk" || { echo "RESULT: install failed"; exit 1; }
adb shell monkey -p "$pkg" -c android.intent.category.LAUNCHER 1
alive=no
mic=unknown
# Up to 8 minutes: the voice and listening files download first, then the microphone opens.
for i in $(seq 1 96); do
  sleep 5
  if adb shell pidof "$pkg" >/dev/null 2>&1; then alive=yes; else alive=no; break; fi
  log=$(adb logcat -d -v brief | grep "spike:" || true)
  if echo "$log" | grep -q "microphone open"; then mic=open; break; fi
  if echo "$log" | grep -q "no microphone"; then mic=failed; break; fi
done
echo "after $((i*5))s: running=$alive microphone=$mic"
echo "===== the app's own readout lines ====="
adb logcat -d -v brief | grep "spike:" | sed 's/.*spike: //' | tail -40
echo "===== crash and app log lines ====="
adb logcat -d -v brief | grep -E "AndroidRuntime|FATAL|DEBUG   |linker|RustStdoutStderr|panicked|Tauri|Wry|jarvis|SpikeService|dlopen|UnsatisfiedLink|ActivityManager.*$pkg|ActivityTaskManager.*$pkg" | tail -250
echo "===== screen ====="
adb exec-out screencap -p > /tmp/screen.png || true
if command -v convert >/dev/null && [ -s /tmp/screen.png ]; then
  convert /tmp/screen.png -resize 360x -quality 55 /tmp/screen.jpg && echo "SCREEN_B64_BEGIN" && base64 -w 0 /tmp/screen.jpg && echo && echo "SCREEN_B64_END"
fi
echo "RESULT: running=$alive microphone=$mic"
[ "$alive" = yes ] && [ "$mic" = open ]
