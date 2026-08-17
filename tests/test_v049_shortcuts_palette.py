"""Focused contracts for the v0.4.9 pending shortcut and signal palette."""

import json
import unittest
from pathlib import Path

from annotation_app.app import APP_VERSION, HTML
from annotation_app.umap_page import UMAP_HTML


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SIGNAL_COLORS = {
    "G1": "#6929C4",
    "G2": "#8A3800",
    "R1": "#005D5D",
    "R2": "#9F1853",
    "ms760": "#1192E8",
    "ms782": "#393939",
}


def function_body(source: str, name: str, next_name: str) -> str:
    return source.split(f"function {name}", 1)[1].split(
        f"function {next_name}", 1
    )[0]


class V049ShortcutAndPaletteContractTest(unittest.TestCase):
    def test_d_saves_a_pending_manual_cell_pair(self):
        self.assertEqual(APP_VERSION, "lma_studio_v0.4.9")
        self.assertIn(
            'id="createManualPending" class="small-button secondary" '
            'style="display:none;" aria-keyshortcuts="D"',
            HTML,
        )
        self.assertIn("D Save pending", HTML)
        body = function_body(
            HTML,
            "handleManualKeyboardShortcut",
            "syncBootstrapMode",
        )
        self.assertIn("key === 'd'", body)
        self.assertIn("createManualTriplet('pending')", body)
        self.assertIn("state.stage !== 'event_annotation'", body)
        self.assertIn("state.manualAnnotationKind !== 'cell'", body)
        self.assertIn("!state.manualMode", body)

    def test_track_and_umap_receive_one_shared_signal_palette(self):
        palette_module = REPO_ROOT / "annotation_app" / "visual_palette.py"
        self.assertTrue(palette_module.is_file())
        app_source = (REPO_ROOT / "annotation_app" / "app.py").read_text(
            encoding="utf-8"
        )
        umap_source = (REPO_ROOT / "annotation_app" / "umap_page.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("from annotation_app.visual_palette import", app_source)
        self.assertIn("from annotation_app.visual_palette import", umap_source)

        encoded = json.dumps(
            EXPECTED_SIGNAL_COLORS,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        declaration = f"const SIGNAL_COLORS = {encoded};"
        self.assertIn(declaration, HTML)
        self.assertIn(declaration, UMAP_HTML)
        self.assertNotIn("__LMA_SIGNAL_COLORS__", HTML)
        self.assertNotIn("__LMA_SIGNAL_COLORS__", UMAP_HTML)

        track_body = function_body(HTML, "colorForChannel", "tracksForCurrentProject")
        self.assertIn("SIGNAL_COLORS[channel]", track_body)
        umap_body = function_body(UMAP_HTML, "stableColor", "pointColor")
        self.assertIn("SIGNAL_COLORS[name]", umap_body)

    def test_signal_colors_are_distinct_and_readable_on_white(self):
        colors = list(EXPECTED_SIGNAL_COLORS.values())
        self.assertEqual(len(colors), len(set(colors)))

        def luminance(hex_color: str) -> float:
            components = [
                int(hex_color[index : index + 2], 16) / 255.0
                for index in (1, 3, 5)
            ]
            linear = [
                value / 12.92
                if value <= 0.04045
                else ((value + 0.055) / 1.055) ** 2.4
                for value in components
            ]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        for label, color in EXPECTED_SIGNAL_COLORS.items():
            contrast_on_white = 1.05 / (luminance(color) + 0.05)
            with self.subTest(label=label, color=color):
                self.assertGreaterEqual(contrast_on_white, 3.0)


if __name__ == "__main__":
    unittest.main()
