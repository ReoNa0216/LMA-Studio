from __future__ import annotations

import unittest

from annotation_app.app import HTML
from annotation_app.desktop import DesktopApi


class EventHook:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in list(self.handlers):
            handler()


class WindowEvents:
    def __init__(self) -> None:
        self.closed = EventHook()


class NativeWindow:
    def __init__(self) -> None:
        self.events = WindowEvents()
        self.restore_count = 0
        self.show_count = 0
        self.destroy_count = 0

    def restore(self) -> None:
        self.restore_count += 1

    def show(self) -> None:
        self.show_count += 1

    def destroy(self) -> None:
        self.destroy_count += 1


class DynamicWebView:
    def __init__(self) -> None:
        self.windows = []

    def create_window(self, *args, **kwargs):
        window = NativeWindow()
        self.windows.append((args, kwargs, window))
        return window


class Server:
    url = "http://127.0.0.1:43123/"


class NativeUmapBridgeContractTest(unittest.TestCase):
    def test_bridge_dynamically_creates_visible_same_server_umap_window(self):
        webview = DynamicWebView()
        api = DesktopApi(Server(), webview)

        result = api.open_umap("http://127.0.0.1:43123/umap")

        self.assertTrue(result["ok"])
        self.assertTrue(result["created"])
        self.assertEqual(len(webview.windows), 1)
        args, kwargs, _window = webview.windows[0]
        self.assertEqual(args[1], "http://127.0.0.1:43123/umap")
        self.assertNotIn("hidden", kwargs)
        self.assertEqual(kwargs["width"], 1200)
        self.assertEqual(kwargs["height"], 800)
        self.assertTrue(kwargs["resizable"])

    def test_bridge_reuses_open_window_and_recreates_after_user_closes_it(self):
        webview = DynamicWebView()
        api = DesktopApi(Server(), webview)
        first = api.open_umap("http://127.0.0.1:43123/umap")
        first_window = webview.windows[0][2]

        reused = api.open_umap("http://127.0.0.1:43123/umap")
        first_window.events.closed.fire()
        recreated = api.open_umap("http://127.0.0.1:43123/umap")

        self.assertTrue(first["created"])
        self.assertFalse(reused["created"])
        self.assertEqual(first_window.restore_count, 1)
        self.assertEqual(first_window.show_count, 1)
        self.assertTrue(recreated["created"])
        self.assertEqual(len(webview.windows), 2)

    def test_bridge_rejects_external_or_wrong_route_urls_before_creation(self):
        webview = DynamicWebView()
        api = DesktopApi(Server(), webview)

        for unsafe in (
            "https://example.com/umap",
            "http://127.0.0.1:43124/umap",
            "http://127.0.0.1:43123/",
            "http://127.0.0.1:43123/umap?redirect=https://example.com",
        ):
            with self.subTest(url=unsafe), self.assertRaises(ValueError):
                api.open_umap(unsafe)

        self.assertEqual(webview.windows, [])

    def test_frontend_passes_full_origin_url_to_native_bridge_without_window_open(self):
        start = HTML.index("function waitForNativeUmapBridge")
        end = HTML.index("function selectedRawInputMode", start)
        umap_bridge = HTML[start:end]

        self.assertIn("window.location.origin", umap_bridge)
        self.assertIn("window.pywebview.api.open_umap(umapUrl)", umap_bridge)
        self.assertIn("pywebviewready", umap_bridge)
        self.assertIn("waitForNativeUmapBridge", umap_bridge)
        self.assertNotIn("window.pywebview.api.open_umap_window", umap_bridge)
        self.assertNotIn("window.open(", umap_bridge)
        self.assertNotIn("link.target", umap_bridge)
        self.assertNotIn("document.createElement('a')", umap_bridge)


if __name__ == "__main__":
    unittest.main()
