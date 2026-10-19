# FAB Matrix — Android scanner

Native CameraX companion app for the FAB Matrix backend. Scanning on the phone gives
much sharper frames than browser video. The collector code in a card's footer is tiny,
and in a browser stream it was too soft to OCR.

## How it works

1. On the web app's **Scan** page, click **Pair phone** to get a short pair code.
2. Enter the code in the app. It's saved, and the pairing row hides itself afterwards.
3. Pass cards through the on-screen guide. The app waits until the footer strip is sharp
   enough (a Laplacian sharpness gate), then posts three crops to `POST /scan/native`:
   - `footer_crop`: a broad band along the bottom, used for exact collector-code OCR
   - `title_crop`: the title strip, used for fuzzy name OCR
   - `full_image`: the whole card, kept only as a debug sample
4. Matches show up live on the paired web page, which polls `/scan/records`.

Server-side decision order: an exact footer `display_id` read (with corrections for
common OCR confusions such as `R05` → `ROS`), then a strong title match (only when the
server has `easyocr` installed). Partial codes are refused rather than guessed.

Also included: torch, a fixed 2× zoom so the camera can sit at a sharper focus distance,
tap-to-refocus, and an **Advanced** panel that shows the API
URL and the live sharpness readout.

## API address

The default is `https://fabmatrix.t4ngo.com`. At launch the app also reads the current
URL from a public gist that `start_fab.py` keeps up to date, so the app keeps working if
the backend ever falls back to a temporary tunnel. An override set under **Advanced**
wins for the rest of the session.

## Build

Requires Android Studio (for the SDK and its bundled JDK). minSdk 26, targetSdk 35.

```bash
JAVA_HOME=/snap/android-studio/current/jbr \
GRADLE_USER_HOME=$PWD/.gradle \
./gradlew :app:assembleDebug

adb install -r app/build/outputs/apk/debug/app-debug.apk
```

`./debug_phone.sh` builds, installs, launches and tails the logs on a connected phone.
The backend also serves the latest debug build at `GET /scanner-apk`. The web Scan page
links to it, so testers don't need Android Studio.

Debug package id: `com.fabscanner.app.debug`. Scan crops and metadata are saved
server-side under `tmp/scan_debug_samples/` when `debug_save` is set.
