#!/bin/zsh
#
# Archive HealthSync and export an ad-hoc .ipa into distrib/, which is where
# tools/ota/ota_server.py looks — it serves the newest .ipa under that directory
# by mtime.
#
# This exists because bumping CURRENT_PROJECT_VERSION ships nothing on its own.
# distrib/ is gitignored and starts out absent, so between a committed fix and
# the phone there was no step that anything or anyone actually ran: build 45
# fixed humidity scaling on 2026-08-29 and was still not installed a week later.
#
# Usage: zsh scripts/build_ipa.sh
#
# Signing is automatic (-allowProvisioningUpdates), so the ad-hoc profile is
# created or refreshed as needed. The phone must already be registered to the
# team, or the export succeeds and the install silently will not.
set -e

REPO=${0:A:h:h}
ARCHIVE=$REPO/tmp/App.xcarchive
OPTIONS=$REPO/tmp/ExportOptions.plist

mkdir -p "$REPO/tmp"
rm -rf "$ARCHIVE"

cat > "$OPTIONS" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- "release-testing" is what Xcode 15+ calls ad-hoc. -->
    <key>method</key><string>release-testing</string>
    <key>teamID</key><string>PU9QJY3H6Q</string>
    <key>signingStyle</key><string>automatic</string>
    <key>compileBitcode</key><false/>
    <key>stripSwiftSymbols</key><true/>
</dict>
</plist>
PLIST

echo "== archive =="
xcodebuild -project "$REPO/ios/App/App.xcodeproj" -scheme App \
    -configuration Release -destination 'generic/platform=iOS' \
    -archivePath "$ARCHIVE" -allowProvisioningUpdates archive

echo "== export =="
xcodebuild -exportArchive -archivePath "$ARCHIVE" \
    -exportOptionsPlist "$OPTIONS" \
    -exportPath "$REPO/distrib" -allowProvisioningUpdates

# The build number is the thing to check, and the only place it can be read
# back from is the bundle: a delta will not report it until the phone has
# actually installed this.
echo "== built =="
"$REPO/.venv/bin/python" - "$REPO/distrib/App.ipa" <<'PY'
import plistlib, re, sys, zipfile

with zipfile.ZipFile(sys.argv[1]) as ipa:
    name = next(n for n in ipa.namelist()
                if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n))
    info = plistlib.loads(ipa.read(name))
print(f"{info['CFBundleIdentifier']} {info['CFBundleShortVersionString']} "
      f"({info['CFBundleVersion']})")
PY
