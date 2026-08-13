# macOS ARM64 Packaging

The macOS package is a native Cocoa/WebKit window around the same local LMA Studio application server used by the Windows build. It does not change project formats or annotation algorithms.

## Supported Build Host

- Apple Silicon (`arm64`) macOS runner or machine
- macOS 12 or later
- Python 3.11 ARM64

Build from the repository root:

```bash
LMA_STUDIO_VERSION=v0.4.5 bash packaging/macos/build_macos.sh
```

Output:

```text
release/LMA-Studio-v0.4.5-macos-arm64.zip
```

The public package is ad-hoc signed because this project does not currently have an Apple Developer ID certificate or notarization credentials. Gatekeeper may therefore require Control-clicking the app and choosing **Open** on first launch.

The desktop runtime is pinned to pywebview 6.2.1. LMA Studio starts with only the main Cocoa window. The UMAP toolbar action waits for the documented `pywebviewready` event, calls the Python bridge with the full same-server `/umap` URL, and never falls back to Chrome or another external browser. Python uses `webview.create_window` during the running GUI loop to create a true second native window. Because pywebview 6.2.1 generates declared bridge functions with `new Function`, only the native main window receives a random capability URL whose CSP permits that bridge operation; ordinary browser pages and every API response keep the stricter CSP. The capability is process-local and is never stored in a project. The bridge rejects external URLs and reuses an open UMAP window; after the user closes it, the next click creates a new one. The packaged `--check-umap-window` probe invokes `open_umap` through JavaScript in the main WebView before it exercises reuse, close, and recreate. This native-window lifecycle was also validated by mouse on a real Apple Silicon Mac before the v0.4.3 release and remains covered by the packaged probe.
