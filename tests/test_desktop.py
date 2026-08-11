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
    _create_preloaded_umap_window,
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


class FakeCocoaNativeWindow:
    def __init__(self, *, visible=False, miniaturized=False, focus_on_show=True):
        self.visible = visible
        self.miniaturized = miniaturized
        self.key = False
        self.focus_on_show = focus_on_show
        self.collection_behavior = 0
        self.show_count = 0

    def collectionBehavior(self):
        return self.collection_behavior

    def setCollectionBehavior_(self, value):
        self.collection_behavior = value

    def isMiniaturized(self):
        return self.miniaturized

    def deminiaturize_(self, _sender):
        self.miniaturized = False

    def makeKeyAndOrderFront_(self, _sender):
        self.show_count += 1
        if self.focus_on_show:
            self.visible = True
            self.key = True

    def orderFrontRegardless(self):
        if self.focus_on_show:
            self.visible = True

    def orderOut_(self, _sender):
        self.visible = False
        self.key = False

    def isVisible(self):
        return self.visible

    def isKeyWindow(self):
        return self.key


class FakeCocoaApplication:
    def __init__(self):
        self.activation_count = 0

    def activateIgnoringOtherApps_(self, _value):
        self.activation_count += 1


class FakeCocoaAppKit:
    NSWindowCollectionBehaviorMoveToActiveSpace = 1 << 1
    application = FakeCocoaApplication()

    class NSApplication:
        @classmethod
        def sharedApplication(cls):
            return FakeCocoaAppKit.application


class FakeCocoaFoundation:
    YES = True


class ImmediateAppHelper:
    calls = []

    @classmethod
    def callAfter(cls, func, *args):
        cls.calls.append((func, args))
        func(*args)


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
        main = self.windows[0][2]
        auxiliary = self.windows[1][2]
        main.native = FakeCocoaNativeWindow(visible=True)
        auxiliary.native = FakeCocoaNativeWindow()
        main.events.before_show.fire()
        main.events.shown.fire()
        auxiliary.events.before_show.fire()
        auxiliary.events.shown.fire()
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
    def test_umap_child_is_created_hidden_before_the_gui_loop(self):
        webview = FakeWindowFactory()

        window = _create_preloaded_umap_window(webview, FakeServerUrl.url)

        self.assertIs(window, webview.windows[0][2])
        args, kwargs, _created = webview.windows[0]
        self.assertEqual(args[1], "http://127.0.0.1:12345/umap")
        self.assertTrue(kwargs["hidden"])

    def test_js_api_surface_does_not_expose_recursive_state(self) -> None:
        api = DesktopApi(FakeServerUrl(), FakeWindowFactory())

        self.assertTrue(all(name.startswith("_") for name in vars(api)))
        public_methods = {
            name
            for name in dir(api)
            if not name.startswith("_") and callable(getattr(api, name))
        }
        self.assertEqual(public_methods, {"open_umap_window"})

    def test_precreated_umap_window_is_reused_without_dynamic_window_creation(self):
        webview = FakeWindowFactory()
        api = DesktopApi(FakeServerUrl(), webview)
        precreated = FakeAuxWindow()
        api._bind_umap_window(precreated)

        self.assertFalse(api.open_umap_window()["created"])
        self.assertFalse(api.open_umap_window()["created"])
        self.assertEqual(precreated.restore_count, 2)
        self.assertEqual(precreated.show_count, 2)
        self.assertEqual(len(webview.windows), 0)

    def test_macos_open_waits_for_native_ready_and_confirms_visibility(self):
        api = DesktopApi(FakeServerUrl(), FakeWindowFactory())
        precreated = FakeAuxWindow()
        api._bind_umap_window(precreated)
        precreated.events.before_show.fire()

        with mock.patch.object(desktop_module.sys, "platform", "darwin"), mock.patch.object(
            desktop_module,
            "_show_cocoa_window_and_wait",
            return_value={"visible": True, "focused": True},
        ) as show_cocoa:
            result = api.open_umap_window()

        show_cocoa.assert_called_once_with(
            precreated,
            main_window=None,
            timeout=desktop_module.UMAP_WINDOW_OPEN_TIMEOUT_SEC,
        )
        self.assertEqual(precreated.restore_count, 0)
        self.assertEqual(precreated.show_count, 0)
        self.assertTrue(result["visible"])
        self.assertTrue(result["focused"])

    def test_macos_open_rejects_when_native_child_never_becomes_ready(self):
        api = DesktopApi(FakeServerUrl(), FakeWindowFactory())
        precreated = FakeAuxWindow()
        api._bind_umap_window(precreated)

        with mock.patch.object(desktop_module.sys, "platform", "darwin"), mock.patch.object(
            desktop_module,
            "UMAP_NATIVE_READY_TIMEOUT_SEC",
            0.001,
        ), mock.patch.object(desktop_module, "_show_cocoa_window_and_wait") as show_cocoa:
            with self.assertRaisesRegex(RuntimeError, "尚未完成初始化"):
                api.open_umap_window()

        show_cocoa.assert_not_called()

    def test_cocoa_show_is_one_main_loop_transaction_with_visibility_confirmation(self):
        native = FakeCocoaNativeWindow(miniaturized=True)
        auxiliary = mock.Mock(native=native)
        main_native = FakeCocoaNativeWindow(visible=True)
        main = mock.Mock(native=main_native)
        ImmediateAppHelper.calls = []
        FakeCocoaAppKit.application = FakeCocoaApplication()

        with mock.patch.object(
            desktop_module,
            "_load_cocoa_runtime",
            return_value=(FakeCocoaAppKit, FakeCocoaFoundation, ImmediateAppHelper),
        ):
            result = desktop_module._show_cocoa_window_and_wait(
                auxiliary,
                main_window=main,
                timeout=0.1,
            )

        self.assertTrue(result["visible"])
        self.assertTrue(result["focused"])
        self.assertTrue(result["main_visible"])
        self.assertEqual(native.show_count, 1)
        self.assertFalse(native.miniaturized)
        self.assertTrue(
            native.collection_behavior
            & FakeCocoaAppKit.NSWindowCollectionBehaviorMoveToActiveSpace
        )
        self.assertEqual(FakeCocoaAppKit.application.activation_count, 1)
        self.assertGreaterEqual(len(ImmediateAppHelper.calls), 2)

    def test_cocoa_show_rejects_an_acknowledgement_when_window_is_not_visible(self):
        native = FakeCocoaNativeWindow(focus_on_show=False)
        auxiliary = mock.Mock(native=native)
        ImmediateAppHelper.calls = []

        with mock.patch.object(
            desktop_module,
            "_load_cocoa_runtime",
            return_value=(FakeCocoaAppKit, FakeCocoaFoundation, ImmediateAppHelper),
        ):
            with self.assertRaisesRegex(RuntimeError, "没有显示"):
                desktop_module._show_cocoa_window_and_wait(
                    auxiliary,
                    main_window=None,
                    timeout=0.1,
                )

    def test_cocoa_hide_waits_until_the_native_window_is_off_screen(self):
        native = FakeCocoaNativeWindow(visible=True)
        auxiliary = mock.Mock(native=native)
        ImmediateAppHelper.calls = []

        with mock.patch.object(
            desktop_module,
            "_load_cocoa_runtime",
            return_value=(FakeCocoaAppKit, FakeCocoaFoundation, ImmediateAppHelper),
        ):
            result = desktop_module._hide_cocoa_window_and_wait(auxiliary, timeout=0.1)

        self.assertTrue(result["hidden"])
        self.assertFalse(native.visible)
        self.assertGreaterEqual(len(ImmediateAppHelper.calls), 2)

    def test_packaged_macos_probe_opens_hides_and_reopens_the_independent_window(self):
        webview = FakeProbeWebView()
        visible = {"visible": True, "focused": True, "main_visible": True}

        with mock.patch.object(desktop_module.sys, "platform", "darwin"), mock.patch.object(
            desktop_module,
            "_show_cocoa_window_and_wait",
            return_value=visible,
        ) as show_cocoa, mock.patch.object(
            desktop_module,
            "_hide_cocoa_window_and_wait",
            return_value={"hidden": True},
        ) as hide_cocoa:
            result = desktop_module.check_umap_window_runtime(webview_module=webview)

        self.assertEqual(show_cocoa.call_count, 2)
        hide_cocoa.assert_called_once()
        self.assertTrue(result["first_open"]["visible"])
        self.assertTrue(result["reopened"]["visible"])
        self.assertEqual(webview.windows[0][2].destroy_count, 1)
        self.assertEqual(webview.windows[1][2].destroy_count, 1)

    def test_user_close_hides_precreated_umap_window_instead_of_destroying_it(self):
        api = DesktopApi(FakeServerUrl(), FakeWindowFactory())
        precreated = FakeAuxWindow()
        api._bind_umap_window(precreated)

        results = [handler() for handler in precreated.events.closing.handlers]

        self.assertIn(False, results)
        self.assertEqual(precreated.hide_count, 1)
        self.assertEqual(precreated.destroy_count, 0)
        self.assertIs(api._umap_window, precreated)

    def test_closing_main_owned_state_destroys_umap_only(self):
        webview = FakeWindowFactory()
        api = DesktopApi(FakeServerUrl(), webview)
        auxiliary = FakeAuxWindow()
        api._bind_umap_window(auxiliary)

        api._close_umap_window()

        self.assertEqual(auxiliary.destroy_count, 1)
        self.assertIsNone(api._umap_window)

    def test_desktop_precreates_both_native_windows_before_start(self):
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

        self.assertEqual(len(webview.windows), 2)
        self.assertEqual(webview.start_calls[0]["window_count"], 2)
        self.assertNotIn("hidden", webview.windows[0][1])
        self.assertTrue(webview.windows[1][1]["hidden"])
        self.assertEqual(FakeLifecycleServer.last_instance.start_count, 1)
        self.assertEqual(FakeLifecycleServer.last_instance.stop_count, 1)
        self.assertEqual(webview.windows[1][2].destroy_count, 1)

    def test_hidden_umap_creation_failure_stops_server_before_native_loop(self):
        webview = FakeLifecycleWebView(fail_child=True)
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
            with self.assertRaisesRegex(RuntimeError, "无法准备 UMAP 窗口"):
                desktop_module.run_desktop(args, webview_module=webview)

        self.assertEqual(webview.start_calls, [])
        self.assertEqual(FakeLifecycleServer.last_instance.start_count, 0)
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
