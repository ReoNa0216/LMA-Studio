import json
import http.client
from pathlib import Path
import socket
import sys
import unittest
from unittest import mock
from urllib.request import urlopen
import uuid

import annotation_app.desktop as desktop_module
from annotation_app.app import BootstrapAppData, ProjectPaths, RequestActivity
from annotation_app.desktop import (
    DesktopApi,
    DesktopServer,
    SingleInstanceGuard,
    WebViewPathDialog,
    parse_args,
)


class FakeFileDialog:
    OPEN = 10
    FOLDER = 20


class FakeWebView:
    FileDialog = FakeFileDialog


class FakeWindow:
    def __init__(self, selected):
        self.selected = selected
        self.calls = []

    def create_file_dialog(self, dialog_type, **kwargs):
        self.calls.append((dialog_type, kwargs))
        return self.selected


class DesktopPathDialogTest(unittest.TestCase):
    def test_uses_native_folder_dialog(self):
        selected = str(Path.cwd())
        window = FakeWindow((selected,))

        result = WebViewPathDialog(window, FakeWebView())("directory", initial_dir=selected)

        self.assertEqual(result["path"], str(Path(selected).resolve()))
        self.assertFalse(result["cancelled"])
        self.assertEqual(window.calls[0][0], FakeFileDialog.FOLDER)

    def test_uses_role_specific_file_filter(self):
        window = FakeWindow(None)

        result = WebViewPathDialog(window, FakeWebView())("file", file_role="ms")

        self.assertTrue(result["cancelled"])
        self.assertEqual(window.calls[0][0], FakeFileDialog.OPEN)
        self.assertIn("*.txt;*.csv", window.calls[0][1]["file_types"][0])

    def test_uses_cell_event_map_csv_filter(self):
        window = FakeWindow(None)

        WebViewPathDialog(window, FakeWebView())("file", file_role="cell_event_map")

        self.assertIn("*.csv", window.calls[0][1]["file_types"][0])
        self.assertNotIn("*.txt", window.calls[0][1]["file_types"][0])

    def test_rejects_unknown_dialog_kind(self):
        with self.assertRaises(ValueError):
            WebViewPathDialog(FakeWindow(None), FakeWebView())("volume")


class RequestActivityTest(unittest.TestCase):
    def test_tracks_active_write_path(self):
        activity = RequestActivity()

        with activity.track("/api/import-project"):
            self.assertTrue(activity.is_busy())
            self.assertEqual(activity.active_paths(), ("/api/import-project",))

        self.assertFalse(activity.is_busy())


class DesktopServerTest(unittest.TestCase):
    def setUp(self):
        project = ProjectPaths.from_args()
        data = BootstrapAppData(project=project, load_error="", project_selected=False)
        self.server = DesktopServer(data)
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def test_random_loopback_port_serves_bootstrap_and_security_headers(self):
        with urlopen(f"{self.server.url}api/meta", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            self.assertTrue(payload["bootstrap"])
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertNotEqual(self.server.httpd.server_port, 8050)

    def test_rejects_untrusted_host(self):
        host, port = self.server.httpd.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request("GET", "/api/meta", headers={"Host": "attacker.invalid"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
        finally:
            connection.close()

    def test_stop_releases_port(self):
        host, port = self.server.httpd.server_address[:2]
        self.server.stop()

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            self.assertNotEqual(probe.connect_ex((host, port)), 0)
        finally:
            probe.close()


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in list(self.handlers):
            handler()


class FakeEvents:
    def __init__(self):
        self.before_show = FakeEvent()
        self.shown = FakeEvent()
        self.loaded = FakeEvent()
        self.closing = FakeEvent()
        self.closed = FakeEvent()


class FakeAuxWindow:
    def __init__(self):
        self.events = FakeEvents()
        self.restore_count = 0
        self.show_count = 0
        self.hide_count = 0
        self.destroy_count = 0

    def restore(self):
        self.restore_count += 1

    def show(self):
        self.show_count += 1

    def hide(self):
        self.hide_count += 1

    def destroy(self):
        self.destroy_count += 1
        self.events.closed.fire()


class FakeWindowFactory:
    def __init__(self):
        self.windows = []

    def create_window(self, *args, **kwargs):
        window = FakeAuxWindow()
        self.windows.append((args, kwargs, window))
        return window


class FakeLifecycleWebView(FakeWindowFactory):
    FileDialog = FakeFileDialog

    def __init__(self, *, fail_child=False):
        super().__init__()
        self.settings = {}
        self.fail_child = fail_child
        self.start_calls = []

    def create_window(self, *args, **kwargs):
        if self.fail_child and len(self.windows) == 1:
            return None
        return super().create_window(*args, **kwargs)

    def start(self, **kwargs):
        self.start_calls.append({"kwargs": kwargs, "window_count": len(self.windows)})


class FakeProbeWebView(FakeWindowFactory):
    def __init__(self):
        super().__init__()
        self.settings = {}

    def start(self, *, func, **_kwargs):
        func()


class FakeLifecycleServer:
    url = "http://127.0.0.1:12345/"
    last_instance = None

    def __init__(self, _data):
        type(self).last_instance = self
        self.start_count = 0
        self.stop_count = 0
        self.path_dialog = None

    @property
    def busy(self):
        return False

    def set_path_dialog(self, provider):
        self.path_dialog = provider

    def start(self):
        self.start_count += 1

    def stop(self):
        self.stop_count += 1


class FakeServerUrl:
    url = "http://127.0.0.1:12345/"


class DesktopApiTest(unittest.TestCase):
    def test_js_api_surface_does_not_expose_recursive_state(self) -> None:
        api = DesktopApi(FakeServerUrl(), FakeWindowFactory())

        self.assertTrue(all(name.startswith("_") for name in vars(api)))
        public_methods = {
            name
            for name in dir(api)
            if not name.startswith("_") and callable(getattr(api, name))
        }
        self.assertEqual(public_methods, {"open_umap"})

    def test_umap_window_is_created_during_the_gui_loop_and_then_reused(self):
        webview = FakeWindowFactory()
        api = DesktopApi(FakeServerUrl(), webview)
        first = api.open_umap("http://127.0.0.1:12345/umap")
        window = webview.windows[0][2]
        reused = api.open_umap("http://127.0.0.1:12345/umap")

        self.assertTrue(first["created"])
        self.assertFalse(reused["created"])
        self.assertEqual(window.restore_count, 1)
        self.assertEqual(window.show_count, 1)
        self.assertEqual(len(webview.windows), 1)

    def test_packaged_macos_probe_creates_reuses_and_recreates_the_independent_window(self):
        webview = FakeProbeWebView()

        with mock.patch.object(desktop_module.sys, "platform", "darwin"):
            result = desktop_module.check_umap_window_runtime(webview_module=webview)

        self.assertTrue(result["first_open"]["created"])
        self.assertFalse(result["reused"]["created"])
        self.assertTrue(result["reopened"]["created"])
        self.assertEqual(len(webview.windows), 3)
        self.assertEqual(webview.windows[0][2].destroy_count, 1)
        self.assertEqual(webview.windows[1][2].destroy_count, 1)
        self.assertEqual(webview.windows[2][2].destroy_count, 1)

    def test_user_close_forgets_window_so_next_click_recreates_it(self):
        webview = FakeWindowFactory()
        api = DesktopApi(FakeServerUrl(), webview)
        api.open_umap("http://127.0.0.1:12345/umap")
        first = webview.windows[0][2]

        first.events.closed.fire()
        reopened = api.open_umap("http://127.0.0.1:12345/umap")

        self.assertTrue(reopened["created"])
        self.assertEqual(len(webview.windows), 2)

    def test_closing_main_owned_state_destroys_umap_only(self):
        webview = FakeWindowFactory()
        api = DesktopApi(FakeServerUrl(), webview)
        api.open_umap("http://127.0.0.1:12345/umap")
        auxiliary = webview.windows[0][2]

        api._close_umap_window()

        self.assertEqual(auxiliary.destroy_count, 1)
        self.assertIsNone(api._umap_window)

    def test_desktop_starts_with_only_the_main_window(self):
        webview = FakeLifecycleWebView()
        args = parse_args([])
        with mock.patch.object(
            desktop_module,
            "DesktopServer",
            FakeLifecycleServer,
        ), mock.patch.object(
            desktop_module,
            "initial_app_data",
            return_value=object(),
        ), mock.patch.object(
            desktop_module,
            "webview2_runtime_version",
            return_value="test-runtime",
        ):
            desktop_module.run_desktop(args, webview_module=webview)

        self.assertEqual(len(webview.windows), 1)
        self.assertEqual(webview.start_calls[0]["window_count"], 1)
        self.assertNotIn("hidden", webview.windows[0][1])
        self.assertEqual(FakeLifecycleServer.last_instance.start_count, 1)
        self.assertEqual(FakeLifecycleServer.last_instance.stop_count, 1)


@unittest.skipUnless(sys.platform == "win32", "Windows mutex semantics")
class SingleInstanceGuardTest(unittest.TestCase):
    def test_second_guard_is_rejected_until_first_releases(self):
        name = f"LMAStudio.Test.{uuid.uuid4().hex}"
        first = SingleInstanceGuard(name)
        second = SingleInstanceGuard(name)
        third = SingleInstanceGuard(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(third.acquire())
        finally:
            first.release()
            second.release()
            third.release()


@unittest.skipIf(sys.platform == "win32", "POSIX file-lock semantics")
class PortableSingleInstanceGuardTest(unittest.TestCase):
    def test_non_windows_guard_rejects_second_instance_until_release(self):
        name = f"LMAStudio.PortableTest.{uuid.uuid4().hex}"
        first = SingleInstanceGuard(name)
        second = SingleInstanceGuard(name)
        third = SingleInstanceGuard(name)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(third.acquire())
        finally:
            first.release()
            second.release()
            third.release()


class DesktopArgumentsTest(unittest.TestCase):
    def test_double_click_defaults_to_no_project(self):
        args = parse_args([])

        self.assertIsNone(args.project_dir)
        self.assertIsNone(args.raw_data_dir)
        self.assertIsNone(args.annotation_db)
        self.assertFalse(args.debug)
        self.assertFalse(args.check_runtime)
        self.assertFalse(args.check_umap_window)

    def test_packaged_umap_probe_flag_is_hidden_but_parseable(self):
        args = parse_args(["--check-umap-window"])

        self.assertTrue(args.check_umap_window)


class PackagedScientificRuntimeProbeTest(unittest.TestCase):
    def test_desktop_webview_runtime_is_pinned_for_reproducible_window_lifecycle(self):
        repository_root = Path(__file__).resolve().parents[1]
        for relative in (
            "packaging/windows/requirements-win.txt",
            "packaging/macos/requirements-macos.txt",
        ):
            with self.subTest(requirements=relative):
                requirements = (repository_root / relative).read_text(encoding="utf-8")
                self.assertIn("pywebview==6.2.1", requirements)

    def test_macos_builder_runs_the_packaged_independent_umap_window_probe(self):
        script = (
            Path(__file__).resolve().parents[1] / "packaging/macos/build_macos.sh"
        ).read_text(encoding="utf-8")

        self.assertIn('"$executable" --check-umap-window', script)

    def test_runtime_probe_exercises_expat_and_dynamic_preprocessing_imports(self):
        probe = getattr(desktop_module, "check_scientific_runtime", None)
        self.assertTrue(callable(probe))

        result = probe()

        self.assertRegex(result["expat_version"], r"^expat_")
        self.assertEqual(
            result["preprocessing_scripts"],
            [
                "run_v3_01_lif_trace_physical_qc.py",
                "run_v3_02_ms_event_calling.py",
            ],
        )
        self.assertEqual(result["lif_detector_tiers"], ["core", "weak"])
        self.assertEqual(result["project_storage_layout"], "portable_project")

    def test_windows_builder_prioritizes_selected_python_environment_dlls(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "packaging/windows/build_windows.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("Library\\bin", script)
        self.assertRegex(script, r"\$env:PATH\s*=")
        self.assertRegex(script, r"libexpat\.dll")
        self.assertIn("validate_bundle_runtime.py", script)

    def test_windows_bundle_includes_versioned_lif_detector_module(self):
        spec = (
            Path(__file__).resolve().parents[1]
            / "packaging/windows/lifms_annotation.spec"
        ).read_text(encoding="utf-8")

        self.assertIn("scripts/v3/lif_peak_detection.py", spec.replace("\\", "/"))
        self.assertIn("scripts/v3/project_storage.py", spec.replace("\\", "/"))

        macos_spec = (
            Path(__file__).resolve().parents[1]
            / "packaging/macos/lifms_annotation_macos.spec"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "scripts/v3/lif_peak_detection.py",
            macos_spec.replace("\\", "/"),
        )
        self.assertIn(
            "scripts/v3/project_storage.py",
            macos_spec.replace("\\", "/"),
        )


if __name__ == "__main__":
    unittest.main()
