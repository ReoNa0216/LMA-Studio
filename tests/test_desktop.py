import json
import http.client
from pathlib import Path
import socket
import sys
import unittest
from urllib.request import urlopen
import uuid

from annotation_app.app import BootstrapAppData, ProjectPaths, RequestActivity
from annotation_app.desktop import (
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


if __name__ == "__main__":
    unittest.main()
