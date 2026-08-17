#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
python_bin="${PYTHON_BIN:-python3}"
version="${LMA_STUDIO_VERSION:-v0.4.9}"

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

run_packaged_probe() {
  local label="$1"
  local timeout_seconds="$2"
  shift 2
  echo "Running packaged ${label} probe (timeout ${timeout_seconds}s)..."
  "$python_bin" - "$label" "$timeout_seconds" "$executable" "$@" <<'PY'
import subprocess
import sys

label = sys.argv[1]
timeout_seconds = int(sys.argv[2])
command = sys.argv[3:]

try:
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        timeout=timeout_seconds,
    )
except subprocess.TimeoutExpired as exc:
    raise SystemExit(
        f"Packaged {label} probe exceeded {timeout_seconds} seconds"
    ) from exc
except subprocess.CalledProcessError as exc:
    raise SystemExit(
        f"Packaged {label} probe failed with exit code {exc.returncode}"
    ) from exc
PY
}

run_packaged_probe "help" 60 --help
run_packaged_probe "scientific runtime" 60 --check-runtime
run_packaged_probe "independent UMAP window" 180 --check-umap-window

mkdir -p release
archive="$repo_root/release/LMA-Studio-${version}-macos-arm64.zip"
ditto -c -k --sequesterRsrc --keepParent "$app_path" "$archive"
archive_name="$(basename "$archive")"
archive_hash="$(shasum -a 256 "$archive" | awk '{print $1}')"
printf '%s  %s\n' "$archive_hash" "$archive_name" >"${archive}.sha256"

echo "Build complete: $archive"
