# Book Listen (Android and iOS)

The pdf2epub audiobook player, built once for Android and iPhone with Expo (React Native, TypeScript).
It replaces the native Android app in `../android` (version 0.4), keeping its package name
`org.pdf2epub.listen`, so it installs over it and keeps its books and listening places.

- `src/app/` the three tabs (Expo Router): `index.tsx` Library, `listen.tsx` Listen, `read.tsx` Read.
- `src/lib/library.ts` the books in the app's document folder, places, speeds and settings (AsyncStorage),
  importing from Google Drive or the phone's files, deleting.
- `src/lib/player.ts` the one `expo-audio` player: background playback, lock-screen controls (15 s back,
  30 s on), saving the place, the sleep timer.
- `src/lib/mp4.ts` reads an M4B's tags, chapter marks, cover and the read-along text the voice worker
  stores in it (`voice-worker/src/pdf2epub_voice/readalong.py`).

## Build an Android APK on this computer

React Native's C++ build fails on Windows when a path has spaces ("pdf to epub") or is too long. So the app
is built from a copy in `C:\bl\mobile`, with the Android SDK from `../tools/android-build/sdk` linked at
`C:\bl\sdk` (a directory junction), and CMake 3.31.6 from the SDK manager (its ninja handles long paths).
In Git Bash:

```bash
R="/c/Users/DanielKoryat/Desktop/Projects/pdf to epub"
export JAVA_HOME="$R/tools/android-build/jdk-17.0.20.1+1" ANDROID_HOME=C:/bl/sdk
export JAVA_TOOL_OPTIONS="-Djdk.net.unixdomain.tmpdir=C:/Windows/Temp -Djava.nio.channels.spi.SelectorProvider=sun.nio.ch.WindowsSelectorProvider"
powershell -Command 'robocopy "C:\Users\DanielKoryat\Desktop\Projects\pdf to epub\mobile" C:\bl\mobile /MIR /XD node_modules android .expo'
cd /c/bl/mobile && npm ci --legacy-peer-deps
npx expo prebuild --platform android               # generates android/ from app.json (not committed)
printf 'sdk.dir=C:/bl/sdk\ncmake.dir=C:/bl/sdk/cmake/3.31.6\n' > android/local.properties
(cd android && ./gradlew assembleRelease)          # android/app/build/outputs/apk/release/app-release.apk
```

The first build takes about 20 minutes (the C++ libraries); after a change to the app's own code, about 2.

Test builds are signed with this computer's debug key (`plugins/with-local-debug-key.js`), the key the
native 0.4 app used. A Google Play release needs its own upload key and an `.aab` (`./gradlew bundleRelease`
or `npx eas-cli build -p android`).

## iPhone

Building for iOS needs a Mac or Expo's cloud builds: `npx eas-cli build -p ios` (needs an Expo account and an
Apple developer account). Nothing in the code is Android-only.
