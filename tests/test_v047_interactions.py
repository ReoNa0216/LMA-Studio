"""Focused UI contracts retained from the v0.4.7 interaction work."""

import unittest

from annotation_app.app import APP_VERSION, HTML
from annotation_app.umap_page import UMAP_HTML


def function_body(source: str, name: str, next_name: str) -> str:
    return source.split(f"function {name}", 1)[1].split(
        f"function {next_name}", 1
    )[0]


class V047InteractionContractTest(unittest.TestCase):
    def test_manual_shortcuts_are_discoverable_and_guarded(self):
        self.assertEqual(APP_VERSION, "lma_studio_v0.5.0")
        self.assertIn('aria-keyshortcuts="S"', HTML)
        self.assertIn('aria-keyshortcuts="A"', HTML)
        self.assertIn("Keys: S Select", HTML)
        self.assertIn('class="shortcut-line"', HTML)
        self.assertIn(
            "Keys: S Select</span><span class=\"shortcut-line\">A Save pair",
            HTML,
        )
        self.assertIn(
            "Keys: S Select</span><span class=\"shortcut-line\">F Save anchor",
            HTML,
        )
        self.assertIn("setAttribute('aria-keyshortcuts', cellMode ? 'A' : 'F')", HTML)
        body = function_body(
            HTML,
            "handleManualKeyboardShortcut",
            "syncBootstrapMode",
        )
        self.assertIn("activeModal", body)
        self.assertIn("input, textarea, select", body)
        self.assertIn("toggleManualMode()", body)
        self.assertIn("createManualTriplet('accepted')", body)
        self.assertIn("state.manualAnnotationKind === 'cell'", body)
        self.assertIn("state.manualAnnotationKind === 'qc'", body)
        self.assertIn("state.stage === 'qc_calibration'", body)
        self.assertIn("key === 'a'", body)
        self.assertIn("key === 'f'", body)
        self.assertNotIn("ev.key === 'Enter'", body)
        self.assertIn(
            "document.addEventListener('keydown', handleManualKeyboardShortcut)",
            HTML,
        )

    def test_time_lookup_highlights_without_changing_umap_view(self):
        body = function_body(UMAP_HTML, "findTimePoints", "clearTimeSearch")
        self.assertIn("matchedTimeEventIds", body)
        self.assertIn("draw();", body)
        self.assertNotIn("fitPointSet", body)
        self.assertNotIn("view.scale", body)
        self.assertNotIn("view.tx", body)
        self.assertNotIn("view.ty", body)

    def test_double_click_ms760_locates_the_same_event_in_umap(self):
        main_body = function_body(
            HTML,
            "locateMsEventInUmap",
            "selectedRawInputMode",
        )
        self.assertIn("openUmapWindow()", main_body)
        self.assertIn("type: 'highlight-event'", main_body)
        self.assertIn("pendingUmapHighlight", main_body)
        self.assertIn("addEventListener('dblclick'", HTML)

        umap_body = function_body(
            UMAP_HTML,
            "highlightTrackEvent",
            "findTimePoints",
        )
        self.assertIn("matchedTimeEventIds = new Set", umap_body)
        self.assertIn("draw();", umap_body)
        self.assertNotIn("fitPointSet", umap_body)
        self.assertNotIn("centerPointWithoutZoom", umap_body)
        self.assertNotIn("view.scale", umap_body)
        self.assertNotIn("view.tx", umap_body)
        self.assertNotIn("view.ty", umap_body)
        self.assertIn("message.type === 'highlight-event'", UMAP_HTML)
        self.assertIn("type: 'umap-ready'", UMAP_HTML)

    def test_calibration_writes_reenable_the_refit_preview_after_refresh(self):
        for name, next_name in (
            ("reviewCandidate", "clearManualAnnotation"),
            ("clearManualAnnotation", "exportAcceptedCsv"),
            ("acceptWindowPendingAutoCandidates", "toggleManualMode"),
            ("createManualTriplet", "setAttrs"),
        ):
            with self.subTest(action=name):
                body = function_body(HTML, name, next_name)
                released = body.rsplit("state.actionBusy = false", 1)[1]
                self.assertIn("renderQcRefitPanel();", released)

        preview = function_body(
            HTML,
            "previewQcAlignmentRefit",
            "applyQcAlignmentRefit",
        )
        self.assertIn("正在计算", preview)
        self.assertIn("aria-busy", preview)


if __name__ == "__main__":
    unittest.main()
