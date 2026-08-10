# macOS ARM64 Packaging

The macOS package is a native Cocoa/WebKit window around the same local LMA Studio application server used by the Windows build. It does not change project formats or annotation algorithms.

## Supported Build Host

- Apple Silicon (`arm64`) macOS runner or machine
- macOS 12 or later
- Python 3.11 ARM64

Build from the repository root:

```bash
LMA_STUDIO_VERSION=v0.4.0-rc4 bash packaging/macos/build_macos.sh
```

Output:

```text
release/LMA-Studio-v0.4.0-rc4-macos-arm64.zip
```

The public package is ad-hoc signed because this project does not currently have an Apple Developer ID certificate or notarization credentials. Gatekeeper may therefore require Control-clicking the app and choosing **Open** on first launch.
