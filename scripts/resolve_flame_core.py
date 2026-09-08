"""Resolve the exact shared-core wheel pinned by this checkout."""
from pathlib import Path
import argparse
import base64
import hashlib
import json
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true", help="download the private release using authenticated gh")
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    lock = json.loads((ROOT / "packaging/flame-ms-core.json").read_text(encoding="utf-8"))
    wheel = args.wheel or ROOT / "build/core-wheel" / lock["wheel"]
    if args.download:
        wheel.parent.mkdir(parents=True, exist_ok=True)
        encoded = os.environ.get("FLAME_MS_CORE_WHEEL_BASE64", "")
        if encoded:
            payload = base64.b64decode(encoded, validate=True)
            if hashlib.sha256(payload).hexdigest() != lock["sha256"]:
                raise SystemExit("Shared-core CI artifact differs from the pinned SHA256")
            wheel.write_bytes(payload)
        else:
            subprocess.run(["gh", "release", "download", lock["release"], "--repo", lock["repository"],
                            "--pattern", lock["wheel"], "--dir", str(wheel.parent), "--clobber"], check=True)
    actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if actual != lock["sha256"]:
        raise SystemExit(f"Shared-core wheel SHA256 mismatch: {actual}")
    print(f"flame-ms-core {lock['version']} commit={lock['commit']} sha256={actual}")
    if args.download and os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as stream:
            stream.write(f"FLAME_MS_CORE_WHEEL={wheel.resolve()}\n")

if __name__ == "__main__":
    main()

