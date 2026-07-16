#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"
version="${LMA_STUDIO_VERSION:-v0.2.0}"

cd "$repo_root"

machine="$($python_bin -c 'import platform; print(platform.machine().lower())')"
if [[ "$machine" != "arm64" ]]; then
  echo "macOS ARM64 packaging requires an arm64 Python interpreter; found: $machine" >&2
  exit 1
fi

"$python_bin" -m pip install --upgrade pip wheel setuptools
"$python_bin" -m pip install -r packaging/macos/requirements-macos.txt
"$python_bin" -m unittest discover -s tests -q

"$python_bin" packaging/macos/generate_icon.py
iconutil -c icns packaging/macos/LMAStudio.iconset -o packaging/macos/LMAStudio.icns

export LMA_STUDIO_VERSION="$version"
"$python_bin" -m PyInstaller --clean --noconfirm packaging/macos/lifms_annotation_macos.spec

app_path="$repo_root/dist/LMA Studio.app"
executable="$app_path/Contents/MacOS/LMAStudio"
if [[ ! -x "$executable" ]]; then
  echo "PyInstaller returned success but did not create $executable" >&2
  exit 1
fi

codesign --force --deep --sign - "$app_path"
codesign --verify --deep --strict "$app_path"
plutil -lint "$app_path/Contents/Info.plist"
file "$executable" | grep -q "arm64"
"$executable" --help >/dev/null

mkdir -p release
archive="$repo_root/release/LMA-Studio-${version}-macos-arm64.zip"
ditto -c -k --sequesterRsrc --keepParent "$app_path" "$archive"
shasum -a 256 "$archive" >"${archive}.sha256"

echo "Build complete: $archive"
