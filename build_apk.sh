#!/usr/bin/env bash
# שימוש: ./mobile/build_apk.sh https://your-app.onrender.com
set -e
SERVER="${1:?כתוב את כתובת השרת, למשל https://your-app.onrender.com}"
cd "$(dirname "$0")"
rm -rf www && mkdir www && cp ../web/index.html www/
printf 'window.APP_CONFIG = { API_BASE: "%s" };\n' "$SERVER" > www/config.js
[ -f package.json ] || npm init -y >/dev/null
npm i @capacitor/core @capacitor/cli @capacitor/android
[ -d android ] || npx cap add android
npx cap sync android
echo "---"
echo "עכשיו: npx cap open android  (נפתח Android Studio) ואז Build > Build APK(s)"
