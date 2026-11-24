# Build the Android APK

This module is a small WebView wrapper around the official TikTok Video Downloader HD website.
It does not contain a copy of the downloader logic — the app loads
`https://tiktokvideodownloaderhd.vercel.app/` and uses the live service.

## Automatic build

Pushes that touch `android/**` run `.github/workflows/build-apk.yml`. The workflow:

1. sets up JDK 17 and the Android SDK,
2. generates a one-off release keystore,
3. builds the signed release APK,
4. uploads the APK and keystore as a workflow artifact.

Download the artifact, keep the keystore somewhere safe, and place the APK in
`public/downloads/` before deploying the website.

## Local build

Requirements: JDK 17, Android SDK platform 35, build-tools 35.0.0, and Gradle 8.14.3.

```bash
keytool -genkeypair -v -keystore android/app/release.keystore -alias tiktokhd \
  -keyalg RSA -keysize 2048 -validity 10000 -storepass tiktokhd2026 -keypass tiktokhd2026 \
  -dname "CN=TikTok Video Downloader HD, O=TikTok Video Downloader HD, C=BD"

gradle -p android assembleRelease
```

The signed APK is written to:

`android/app/build/outputs/apk/release/TikTok-Video-Downloader-HD-1.0.0.apk`

Use the same keystore and alias for future updates, otherwise Android will treat the next
APK as a different app.
