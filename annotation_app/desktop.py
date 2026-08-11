#!/usr/bin/env python3
"""Desktop host for LMA Studio.

The scientific application remains an HTTP/HTML app. This module owns only the
native window, local server lifecycle, single-instance guard, and file dialogs.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
from typing import Any
from urllib.parse import urljoin, urlsplit
import uuid

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from annotation_app.app import (
    APP_DISPLAY_NAME,
    BootstrapAppData,
    ProjectPaths,
    create_http_server,
    initial_app_data,
    load_preprocessing_module,
    native_path_dialog_response,
)


LOGGER = logging.getLogger(__name__)
WINDOWS_ALREADY_EXISTS = 183
WEBVIEW2_CLIENT_IDS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",
    "{65C35B14-6C2D-412B-AC46-7148CC9D6497}",
)


def user_state_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "LMA Studio"


def configure_logging() -> Path:
    log_dir = user_state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "lma-studio.log"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    return log_path


def show_native_message(message: str, *, title: str = APP_DISPLAY_NAME, error: bool = False) -> None:
    if sys.platform == "win32":
        icon = 0x10 if error else 0x30
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x0 | icon)
        return
    print(f"{title}: {message}", file=sys.stderr)


def webview2_runtime_version() -> str | None:
    if sys.platform != "win32":
        return "platform-webview"
    import winreg

    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    views = (0, getattr(winreg, "KEY_WOW64_32KEY", 0), getattr(winreg, "KEY_WOW64_64KEY", 0))
    for client_id in WEBVIEW2_CLIENT_IDS:
        for root in roots:
            for prefix in (r"SOFTWARE\Microsoft\EdgeUpdate\Clients", r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"):
                for view in views:
                    try:
                        with winreg.OpenKey(root, f"{prefix}\\{client_id}", 0, winreg.KEY_READ | view) as key:
                            version = str(winreg.QueryValueEx(key, "pv")[0]).strip()
                            if version and version != "0.0.0.0":
                                return version
                    except OSError:
                        continue
    return None


class SingleInstanceGuard:
    def __init__(self, name: str = "LMAStudio.Desktop") -> None:
        self.name = name
        self._handle: Any = None
        self._lock_file: Any = None

    def acquire(self) -> bool:
        if sys.platform == "win32":
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.CreateMutexW(None, False, f"Local\\{self.name}")
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == WINDOWS_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            self._handle = (kernel32, handle)
            return True

        import fcntl

        lock_dir = user_state_dir()
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_id = uuid.uuid5(uuid.NAMESPACE_URL, self.name).hex
        lock_file = (lock_dir / f"instance-{lock_id}.lock").open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            lock_file.close()
            return False
        self._lock_file = lock_file
        return True

    def release(self) -> None:
        if self._handle is not None:
            kernel32, handle = self._handle
            kernel32.CloseHandle(handle)
            self._handle = None
        if self._lock_file is not None:
            import fcntl

            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_file.close()
                self._lock_file = None

    def __enter__(self) -> "SingleInstanceGuard":
        if not self.acquire():
            raise RuntimeError("LMA Studio is already running")
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


class WebViewPathDialog:
    def __init__(self, window: Any, webview_module: Any) -> None:
        self.window = window
        self.webview = webview_module

    def __call__(self, kind: str, title: str = "", initial_dir: str = "", file_role: str = "") -> dict[str, Any]:
        if kind not in {"directory", "file"}:
            raise ValueError(f"Unsupported native path dialog kind: {kind}")
        initial = Path(initial_dir).expanduser() if str(initial_dir).strip() else Path.home()
        if initial.is_file():
            initial = initial.parent
        directory = str(initial) if initial.is_dir() else ""
        if kind == "directory":
            dialog_type = self.webview.FileDialog.FOLDER
            file_types: tuple[str, ...] = ()
        else:
            dialog_type = self.webview.FileDialog.OPEN
            if file_role == "ms":
                file_types = ("MS raw files (*.txt;*.csv)", "All files (*.*)")
            elif file_role == "cell_event_map":
                file_types = ("Cell event coordinate CSV (*.csv)", "All files (*.*)")
            else:
                file_types = ("LIF raw files (*.csv;*.txt)", "All files (*.*)")
        selected = self.window.create_file_dialog(
            dialog_type,
            directory=directory,
            allow_multiple=False,
            file_types=file_types,
        )
        first = selected[0] if selected else None
        return native_path_dialog_response(kind, lambda: first)


class DesktopServer:
    def __init__(self, data: Any, host: str = "127.0.0.1", port: int = 0) -> None:
        self.httpd = create_http_server(host, port, data)
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/"

    @property
    def busy(self) -> bool:
        return self.httpd.request_activity.is_busy()

    @property
    def active_paths(self) -> tuple[str, ...]:
        return self.httpd.request_activity.active_paths()

    def set_path_dialog(self, provider: WebViewPathDialog) -> None:
        self.httpd.RequestHandlerClass.path_dialog = staticmethod(provider)

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="lma-local-http", daemon=True)
        self.thread.start()
        LOGGER.info("Local application server started at %s", self.url)

    def stop(self) -> None:
        thread = self.thread
        if thread is not None and thread.is_alive():
            self.httpd.shutdown()
            thread.join(timeout=10)
        self.httpd.server_close()
        self.thread = None
        LOGGER.info("Local application server stopped")


class DesktopApi:
    """Minimal pywebview bridge for one dynamically-created UMAP window."""

    def __init__(self, server: DesktopServer, webview_module: Any) -> None:
        # pywebview recursively inspects public attributes on ``js_api``. Keep
        # state private so only ``open_umap`` is exposed to JavaScript.
        self._server = server
        self._webview = webview_module
        self._umap_window: Any | None = None
        self._lock = threading.RLock()

    def _validated_umap_url(self, requested_url: str) -> str:
        """Allow the bridge to open only this process's local ``/umap`` page."""

        expected = urljoin(str(self._server.url), "umap")
        expected_parts = urlsplit(expected)
        requested_parts = urlsplit(str(requested_url).strip())
        same_endpoint = (
            requested_parts.scheme == expected_parts.scheme
            and requested_parts.hostname == expected_parts.hostname
            and requested_parts.port == expected_parts.port
            and requested_parts.path == expected_parts.path
            and not requested_parts.username
            and not requested_parts.password
            and not requested_parts.query
            and not requested_parts.fragment
        )
        if not same_endpoint:
            raise ValueError("只能打开当前 LMA Studio 项目的 UMAP 窗口")
        return expected

    def _forget_umap_window(self, window: Any) -> None:
        with self._lock:
            if self._umap_window is window:
                self._umap_window = None

    def open_umap(self, url: str) -> dict[str, Any]:
        """Create the native UMAP window during the running GUI loop."""

        safe_url = self._validated_umap_url(url)
        with self._lock:
            existing = self._umap_window
            if existing is not None:
                try:
                    existing.restore()
                except Exception:
                    LOGGER.debug("UMAP window restore is unavailable", exc_info=True)
                try:
                    existing.show()
                except Exception as exc:
                    raise RuntimeError("无法显示 UMAP 窗口，请关闭后重试") from exc
                return {"ok": True, "created": False}

            window = self._webview.create_window(
                f"{APP_DISPLAY_NAME} · UMAP",
                safe_url,
                width=1200,
                height=800,
                min_size=(640, 480),
                resizable=True,
                text_select=True,
                zoomable=True,
                background_color="#f7f8fa",
            )
            if window is None:
                raise RuntimeError("无法创建 UMAP 窗口，请重试")
            self._umap_window = window

            def forget(*_args: Any) -> None:
                self._forget_umap_window(window)

            window.events.closed += forget
            return {"ok": True, "created": True}

    def _close_umap_window(self, *_args: Any) -> None:
        with self._lock:
            window = self._umap_window
            self._umap_window = None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                LOGGER.debug("UMAP window was already closed", exc_info=True)


def check_umap_window_runtime(*, webview_module: Any | None = None) -> dict[str, Any]:
    """Create the UMAP window dynamically in a packaged macOS GUI loop."""

    if sys.platform != "darwin":
        raise RuntimeError("独立 UMAP 窗口探针只适用于 macOS")
    if webview_module is None:
        import webview as webview_module

    project = ProjectPaths.from_args()
    data = BootstrapAppData(project=project, load_error="", project_selected=False)
    server = DesktopServer(data)
    desktop_api = DesktopApi(server, webview_module)
    webview_module.settings["ALLOW_DOWNLOADS"] = False
    webview_module.settings["SHOW_DEFAULT_MENUS"] = False
    main_window = webview_module.create_window(
        f"{APP_DISPLAY_NAME} · Window check",
        server.url,
        width=640,
        height=420,
        min_size=(480, 320),
        resizable=True,
        background_color="#f6f7f9",
        js_api=desktop_api,
    )
    if main_window is None:
        raise RuntimeError("无法创建 macOS 主窗口探针")

    result: dict[str, Any] = {}
    failures: list[BaseException] = []

    def exercise_windows() -> None:
        try:
            umap_url = urljoin(server.url, "umap")
            first_open = desktop_api.open_umap(umap_url)
            first_window = desktop_api._umap_window
            reused = desktop_api.open_umap(umap_url)
            if first_window is None or not first_open.get("created") or reused.get("created"):
                raise RuntimeError("macOS UMAP 原生窗口未按预期动态创建或复用")
            first_window.destroy()
            desktop_api._forget_umap_window(first_window)
            reopened = desktop_api.open_umap(umap_url)
            if not reopened.get("created") or desktop_api._umap_window is first_window:
                raise RuntimeError("macOS UMAP 原生窗口关闭后未能重新创建")
            result.update(first_open=first_open, reused=reused, reopened=reopened)
        except BaseException as exc:
            failures.append(exc)
        finally:
            desktop_api._close_umap_window()
            try:
                main_window.destroy()
            except Exception as exc:
                if not failures:
                    failures.append(exc)

    server.start()
    try:
        webview_module.start(func=exercise_windows, debug=False, private_mode=True)
        if failures:
            raise RuntimeError(f"macOS 独立 UMAP 窗口探针失败：{failures[0]}") from failures[0]
        if not result:
            raise RuntimeError("macOS 独立 UMAP 窗口探针没有返回结果")
        return result
    finally:
        desktop_api._close_umap_window()
        server.stop()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=f"运行 {APP_DISPLAY_NAME} 桌面应用。")
    parser.add_argument("--project-dir", default=None)
    parser.add_argument("--raw-data-dir", default=None)
    parser.add_argument("--annotation-db", default=None)
    parser.add_argument("--debug", action="store_true", help="启用 WebView 开发工具，仅用于开发调试。")
    parser.add_argument("--check-runtime", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check-umap-window", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def check_scientific_runtime() -> dict[str, Any]:
    """Exercise binary-backed imports used after the desktop has started.

    A plain GUI startup probe does not import ``pyexpat`` or the dynamically
    loaded preprocessing scripts.  Keeping these imports here makes a packaged
    ``--check-runtime`` fail during the build instead of when a user first
    clicks the calibration-window suggestion button.
    """

    import bz2  # noqa: F401
    import lzma  # noqa: F401
    import sqlite3
    import ssl
    from xml.parsers import expat

    # ``ctypes`` is imported at module load; touch its binary loader here so a
    # missing libffi is caught by the packaged probe as well.
    if not callable(getattr(ctypes, "CDLL", None)):
        raise RuntimeError("ctypes binary loader is unavailable")

    script_names = [
        "run_v3_01_lif_trace_physical_qc.py",
        "run_v3_02_ms_event_calling.py",
    ]
    loaded_scripts: dict[str, Any] = {}
    for script_name in script_names:
        module = load_preprocessing_module(script_name)
        if not callable(getattr(module, "run", None)):
            raise RuntimeError(
                f"Bundled preprocessing script has no run(project_dir=...) entry: {script_name}"
            )
        loaded_scripts[script_name] = module

    # Import success alone does not exercise scipy.signal or the detector-v2
    # dependency chain. Run one tiny, deterministic core+weak call without
    # touching a project or the filesystem.
    import numpy as np
    import pandas as pd
    from scripts.v3.project_storage import (
        CANONICAL_STORAGE_LAYOUT_NAME,
        canonical_storage_layout_manifest_entry,
    )

    storage_layout = canonical_storage_layout_manifest_entry()
    if storage_layout.get("name") != CANONICAL_STORAGE_LAYOUT_NAME:
        raise RuntimeError("Bundled project storage contract is unavailable")

    lif_module = loaded_scripts["run_v3_01_lif_trace_physical_qc.py"]
    time_sec = np.linspace(0.0, 1.0, 1001)
    signal = sum(
        15.0 * np.exp(-0.5 * ((time_sec - center) / 0.015) ** 2)
        for center in (0.15, 0.30, 0.45)
    ) + 6.0 * np.exp(-0.5 * ((time_sec - 0.72) / 0.015) ** 2)
    detector_config = {
        "detector_version": 2,
        "profile": "core_weak",
        "core": {"prominence_snr_min": 10.0},
        "weak": {"enabled": True, "prominence_snr_min": 3.5},
        "geometry": {
            "min_distance_sec": 0.02,
            "merge_gap_sec": 0.12,
            "min_width_sec": 0.02,
            "max_width_sec": 1.0,
        },
        "weak_usage": "manual_review_only",
    }
    trace = pd.DataFrame(
        {
            "channel": "G1",
            "label": "runtime_probe",
            "detector": "green",
            "phase": "runtime_probe",
            "time_min": time_sec / 60.0,
            "time_sec": time_sec,
            "raw": signal,
            "baseline": np.zeros_like(signal),
            "signal": signal,
        }
    )
    raw_peaks = lif_module.call_raw_peaks(
        trace,
        {"dt_sec": float(time_sec[1] - time_sec[0]), "noise": 1.0},
        detection_config=detector_config,
    )
    detector_tiers = sorted(set(raw_peaks.get("peak_tier", pd.Series(dtype=str))))
    if detector_tiers != ["core", "weak"]:
        raise RuntimeError(
            "Bundled detector-v2 scientific probe did not emit core and weak tiers"
        )

    return {
        "expat_version": str(expat.EXPAT_VERSION),
        "openssl_version": str(ssl.OPENSSL_VERSION),
        "sqlite_version": str(sqlite3.sqlite_version),
        "preprocessing_scripts": script_names,
        "lif_detector_tiers": detector_tiers,
        "project_storage_layout": str(storage_layout["name"]),
    }


def check_desktop_runtime() -> None:
    check_scientific_runtime()
    if sys.platform == "win32":
        if webview2_runtime_version() is None:
            raise RuntimeError("未检测到 Microsoft Edge WebView2 Runtime。")
        import webview.platforms.winforms  # noqa: F401
        return
    if sys.platform == "darwin":
        import webview.platforms.cocoa  # noqa: F401


def run_desktop(args: argparse.Namespace, *, webview_module: Any | None = None) -> None:
    if sys.platform == "win32" and webview2_runtime_version() is None:
        raise RuntimeError("未检测到 Microsoft Edge WebView2 Runtime。请安装 WebView2 Runtime 后重新启动 LMA Studio。")

    if webview_module is None:
        import webview as webview_module

    project_selected = any([args.project_dir, args.raw_data_dir, args.annotation_db])
    project = ProjectPaths.from_args(
        project_dir=args.project_dir,
        raw_data_dir=args.raw_data_dir,
        annotation_db=args.annotation_db,
    )
    data = initial_app_data(project, project_selected=project_selected)
    server = DesktopServer(data)
    desktop_api = DesktopApi(server, webview_module)
    webview_module.settings["ALLOW_DOWNLOADS"] = True
    webview_module.settings["SHOW_DEFAULT_MENUS"] = False
    window = webview_module.create_window(
        APP_DISPLAY_NAME,
        server.url,
        width=1280,
        height=800,
        min_size=(960, 640),
        resizable=True,
        text_select=True,
        zoomable=True,
        background_color="#f6f7f9",
        js_api=desktop_api,
    )
    if window is None:
        server.stop()
        raise RuntimeError("无法创建 LMA Studio 应用窗口")
    server.set_path_dialog(WebViewPathDialog(window, webview_module))

    def block_unsafe_close() -> bool | None:
        if not server.busy:
            return None
        show_native_message("当前正在处理或写入项目，请等待操作完成后再关闭，避免产生不完整项目。")
        return False

    window.events.closing += block_unsafe_close
    window.events.closed += desktop_api._close_umap_window
    server.start()
    try:
        gui = "edgechromium" if sys.platform == "win32" else None
        webview_module.start(gui=gui, debug=bool(args.debug), private_mode=True)
    finally:
        desktop_api._close_umap_window()
        server.stop()


def main(argv: list[str] | None = None) -> int:
    log_path = configure_logging()
    guard = SingleInstanceGuard()
    try:
        args = parse_args(argv)
        if args.check_runtime:
            check_desktop_runtime()
            return 0
        if args.check_umap_window:
            check_umap_window_runtime()
            return 0
        if not guard.acquire():
            show_native_message("LMA Studio 已经在运行。请切换到现有窗口。")
            return 2
        run_desktop(args)
        return 0
    except Exception as exc:
        LOGGER.exception("Desktop startup failed")
        show_native_message(f"LMA Studio 无法启动：\n{exc}\n\n诊断日志：{log_path}", error=True)
        return 1
    finally:
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
