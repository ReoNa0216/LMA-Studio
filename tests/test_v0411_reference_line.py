"""Focused contracts for the v0.4.11 cross-track reference line."""

import json
import shutil
import subprocess
import unittest

from annotation_app.app import APP_VERSION, HTML


def function_body(source: str, name: str, next_name: str) -> str:
    return source.split(f"function {name}", 1)[1].split(
        f"function {next_name}", 1
    )[0]


def javascript_function(source: str, name: str, next_name: str) -> str:
    return "function " + name + function_body(source, name, next_name)


class V0411ReferenceLineContractTest(unittest.TestCase):
    def test_reference_line_control_is_adjacent_and_concise(self):
        self.assertEqual(APP_VERSION, "lma_studio_v0.5.1")
        weak_index = HTML.index('id="showWeakLifPeaks"')
        guide_index = HTML.index('id="verticalGuideEnabled"')
        show_index = HTML.index('id="go"')
        self.assertLess(weak_index, guide_index)
        self.assertLess(guide_index, show_index)
        self.assertIn('Reference line', HTML[guide_index : show_index])
        self.assertIn('id="verticalGuideReadout"', HTML)

    def test_reference_line_is_runtime_only_and_stage_independent(self):
        state_block = HTML.split("const state =", 1)[1].split(
            "const stateChannel", 1
        )[0]
        self.assertIn("verticalGuideEnabled: false", state_block)
        self.assertIn("verticalGuidePinned: false", state_block)
        self.assertIn("verticalGuideTimeMin: null", state_block)

        pointer_body = function_body(
            HTML,
            "handleVerticalGuidePointerMove",
            "handleVerticalGuideClick",
        )
        click_body = function_body(
            HTML,
            "handleVerticalGuideClick",
            "handleVerticalGuideKeyboard",
        )
        combined = pointer_body + click_body
        self.assertNotIn("state.stage", combined)
        self.assertNotIn("fetch(", combined)
        self.assertNotIn("postJson", combined)
        self.assertNotIn("project_config", combined)

    def test_pointer_moves_then_click_pins_and_line_click_releases(self):
        pointer_body = function_body(
            HTML,
            "handleVerticalGuidePointerMove",
            "handleVerticalGuideClick",
        )
        self.assertIn("state.verticalGuideEnabled", pointer_body)
        self.assertIn("state.verticalGuidePinned", pointer_body)
        self.assertIn("verticalGuideTimeFromPointer", pointer_body)
        self.assertIn("renderVerticalGuide()", pointer_body)

        click_body = function_body(
            HTML,
            "handleVerticalGuideClick",
            "handleVerticalGuideKeyboard",
        )
        self.assertIn("vertical-guide-hit", click_body)
        self.assertIn("state.verticalGuidePinned = false", click_body)
        self.assertIn("verticalGuideClickBlocked", click_body)
        self.assertIn("state.verticalGuidePinned = true", click_body)

    def test_reference_line_does_not_steal_peak_or_connector_clicks(self):
        blocked_body = function_body(
            HTML,
            "verticalGuideClickBlocked",
            "verticalGuideTimeFromPointer",
        )
        self.assertIn("target.__detail", blocked_body)
        self.assertIn("closest('[tabindex]')", blocked_body)
        self.assertIn("weak-peak-hit-target", blocked_body)

        click_body = function_body(
            HTML,
            "handleVerticalGuideClick",
            "handleVerticalGuideKeyboard",
        )
        self.assertLess(
            click_body.index("verticalGuideClickBlocked"),
            click_body.index("state.verticalGuidePinned = true"),
        )

    def test_arrow_keys_nudge_a_pinned_line_by_rendered_pixels(self):
        body = function_body(
            HTML,
            "handleVerticalGuideKeyboard",
            "drawVerticalGuideOverlay",
        )
        self.assertIn("ev.key !== 'ArrowLeft'", body)
        self.assertIn("ev.key !== 'ArrowRight'", body)
        self.assertIn("state.verticalGuidePinned", body)
        self.assertIn("input, textarea, select, button", body)
        self.assertIn("geometry.end - geometry.start", body)
        self.assertIn("geometry.x1 - geometry.x0", body)
        self.assertIn("ev.shiftKey ? 10 : 1", body)
        self.assertIn("ev.preventDefault()", body)

    def test_overlay_spans_all_tracks_and_uses_a_separate_hit_target(self):
        body = function_body(
            HTML,
            "drawVerticalGuideOverlay",
            "renderVerticalGuide",
        )
        disabled_branch = body.split("if (!state.verticalGuideEnabled)", 1)[1].split(
            "return;", 1
        )[0]
        self.assertIn("renderVerticalGuide()", disabled_branch)
        self.assertIn("class: 'vertical-guide-line'", body)
        self.assertIn("class: 'vertical-guide-hit'", body)
        self.assertIn("y1: geometry.y0", body)
        self.assertIn("y2: geometry.y1", body)
        self.assertIn("'pointer-events': 'none'", body)
        self.assertIn("'pointer-events': 'stroke'", body)

        draw_tail = function_body(HTML, "draw", "trackShiftSec")
        self.assertLess(
            draw_tail.index("bringPeakLabelsToFront(svg)"),
            draw_tail.index("drawVerticalGuideOverlay(svg)"),
        )

    def test_disabling_reference_line_removes_the_left_edge_artifact(self):
        body = function_body(HTML, "renderVerticalGuide", "draw")
        disabled_branch = body.split("if (!enabled)", 1)[1].split("return;", 1)[0]
        self.assertIn("line.style.display = 'none'", disabled_branch)
        self.assertIn("hit.style.display = 'none'", disabled_branch)
        self.assertIn("readout.style.display = enabled ? 'inline-block' : 'none'", body)
        self.assertIn("readout.textContent = ''", disabled_branch)

    def test_pointer_pin_release_and_pixel_nudge_execute_as_one_state_machine(self):
        if shutil.which("node") is None:
            self.skipTest("Node.js is needed for the embedded-JS behavior contract")
        functions = "\n".join(
            [
                javascript_function(
                    HTML, "verticalGuideClickBlocked", "verticalGuideTimeFromPointer"
                ),
                javascript_function(
                    HTML, "verticalGuideTimeFromPointer", "handleVerticalGuidePointerMove"
                ),
                javascript_function(
                    HTML, "handleVerticalGuidePointerMove", "handleVerticalGuideClick"
                ),
                javascript_function(
                    HTML, "handleVerticalGuideClick", "handleVerticalGuideKeyboard"
                ),
                javascript_function(
                    HTML, "handleVerticalGuideKeyboard", "drawVerticalGuideOverlay"
                ),
            ]
        )
        script = f"""
class Element {{
  constructor(kind = 'background') {{ this.kind = kind; this.__detail = null; }}
  closest(selector) {{
    if (selector === '.vertical-guide-hit') return this.kind === 'guide' ? this : null;
    if (selector === '.weak-peak-hit-target') return this.kind === 'weak' ? this : null;
    if (selector === '[tabindex]') return this.kind === 'focusable' ? this : null;
    if (selector.includes('input')) return null;
    return null;
  }}
}}
const state = {{
  verticalGuideEnabled: true,
  verticalGuidePinned: false,
  verticalGuideTimeMin: null,
  verticalGuideGeometry: {{width:1000,height:600,x0:100,x1:900,y0:20,y1:560,start:10,end:20}}
}};
let activeModal = null;
let renders = 0;
const chart = {{getBoundingClientRect: () => ({{left:0,top:0,width:1000,height:600}})}};
function el(id) {{ return id === 'chart' ? chart : null; }}
function renderVerticalGuide() {{ renders += 1; }}
{functions}
const event = (target, x = 500, y = 300, key = '') => ({{
  target, clientX:x, clientY:y, key, shiftKey:false,
  prevented:false, stopped:false,
  preventDefault() {{ this.prevented = true; }},
  stopPropagation() {{ this.stopped = true; }}
}});
const background = new Element();
handleVerticalGuidePointerMove(event(background));
const afterMove = state.verticalGuideTimeMin;
handleVerticalGuideClick(event(background));
const afterPin = state.verticalGuidePinned;
const right = event(background, 500, 300, 'ArrowRight');
handleVerticalGuideKeyboard(right);
const afterRight = state.verticalGuideTimeMin;
const guideClick = event(new Element('guide'));
handleVerticalGuideClick(guideClick);
const afterRelease = state.verticalGuidePinned;
state.verticalGuidePinned = false;
const peak = new Element(); peak.__detail = {{kind:'lif_peak'}};
handleVerticalGuideClick(event(peak, 700));
process.stdout.write(JSON.stringify({{afterMove,afterPin,afterRight,afterRelease,peakPinned:state.verticalGuidePinned,rightPrevented:right.prevented,renders}}));
"""
        completed = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        result = json.loads(completed.stdout)
        self.assertAlmostEqual(result["afterMove"], 15.0)
        self.assertTrue(result["afterPin"])
        self.assertAlmostEqual(result["afterRight"], 15.0125)
        self.assertFalse(result["afterRelease"])
        self.assertFalse(result["peakPinned"])
        self.assertTrue(result["rightPrevented"])
        self.assertGreaterEqual(result["renders"], 4)


if __name__ == "__main__":
    unittest.main()
