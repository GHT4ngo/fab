#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADB="${ANDROID_HOME:-$HOME/Android/Sdk}/platform-tools/adb"
JAVA_HOME="${JAVA_HOME:-/snap/android-studio/232/jbr}"
GRADLE_USER_HOME="${GRADLE_USER_HOME:-/tmp/fab-gradle}"
APK="$ROOT_DIR/app/build/intermediates/apk/debug/app-debug.apk"
APP_ID="com.fabscanner.app.debug"
ACTIVITY="com.fabscanner.app.MainActivity"

if [[ ! -x "$ADB" ]]; then
  echo "adb not found at $ADB" >&2
  exit 1
fi

if [[ ! -x "$JAVA_HOME/bin/java" ]]; then
  echo "java not found at $JAVA_HOME/bin/java" >&2
  exit 1
fi

cd "$ROOT_DIR"
PATH="$JAVA_HOME/bin:$PATH" JAVA_HOME="$JAVA_HOME" GRADLE_USER_HOME="$GRADLE_USER_HOME" ./gradlew :app:assembleDebug --quiet

"$ADB" wait-for-device
"$ADB" install -r -t "$APK"
"$ADB" shell pm grant "$APP_ID" android.permission.CAMERA >/dev/null 2>&1 || true
"$ADB" shell am force-stop "$APP_ID"
"$ADB" logcat -c
"$ADB" shell am start -n "$APP_ID/$ACTIVITY" \
  -a android.intent.action.MAIN \
  -c android.intent.category.LAUNCHER

echo
echo "Streaming scanner logs. Press Ctrl-C to stop."
"$ADB" logcat -v time -s FabScanner AndroidRuntime CameraX Camera2CameraImpl ProcessCameraProvider
