# FaB Scanner Android MVP

Native CameraX scanner companion for the FaB backend.

This is intentionally small: one Android activity captures sharper camera frames,
gates on footer sharpness, crops the full card/footer/title regions, and posts
them to the existing backend `/scan/native` endpoint.

## Requirements

- Android Studio
- Android SDK installed through Android Studio
- Android phone with USB debugging enabled
- Local backend reachable from the phone

This workspace currently does not have Java, Gradle, or the Android SDK
installed, so build/run from Android Studio.

## Open

Open this folder in Android Studio:

```bash
/home/tango/Projects/fab/fab-scanner-android
```

Let Android Studio sync Gradle, then run `app` on a connected Android phone.

## Backend URL

Default API base in `MainActivity.kt` is:

```text
http://10.0.2.2:8001
```

That works for the Android emulator. For a physical phone, use either:

- the current quick-tunnel URL, or
- the LAN IP of this machine, e.g. `http://192.168.x.x:8001`

For now, change `apiBase` in `MainActivity.kt` while testing.

## Recognition Signals

The app sends:

- `full_image`: full card guide crop
- `footer_crop`: bottom 7% strip of the card
- `title_crop`: top title strip
- `debug_save: true`

Backend fusion order:

1. footer OCR, exact `display_id`
2. full-card visual match
3. title OCR fuzzy match

## Debug Output

Backend saves native scan debug files to:

```text
/home/tango/Projects/fab/tmp/scan_debug_samples/
```

Each saved image has a matching `.txt` metadata file.

## Current MVP Limits

- The Android app currently parses the backend response with simple regexes.
- It does not yet sync into the web cardlist UI.
- The first target is proving camera quality and recognition confidence.
