"""UI contracts for the standalone UMAP toolbar.

These tests intentionally stay browser-independent.  The UMAP page is a single
embedded HTML document, so its sizing and responsive behaviour can be checked
without starting a desktop window or touching a user project.
"""

from __future__ import annotations

import re
import unittest

from annotation_app.umap_page import UMAP_HTML


def css_rule(selector: str) -> str:
    match = re.search(
        rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}",
        UMAP_HTML,
        re.S,
    )
    if match is None:
        raise AssertionError(f"Missing CSS rule: {selector}")
    return match.group("body")


def readable_min_width(rule: str) -> bool:
    """Return whether a width can show the complete `e.g. 49.001` example."""

    match = re.search(r"min-width\s*:\s*([0-9.]+)\s*(px|ch|rem)", rule)
    if match is None:
        return False
    value = float(match.group(1))
    unit = match.group(2)
    return {
        "px": value >= 120,
        "ch": value >= 12,
        "rem": value >= 7.5,
    }[unit]


class UmapResponsiveLayoutContractTest(unittest.TestCase):
    def test_ms760_time_example_is_complete_and_input_cannot_shrink_below_it(self):
        self.assertRegex(
            UMAP_HTML,
            r'<input[^>]+id="timeQuery"[^>]+placeholder="e\.g\. 49\.001"',
        )

        value_rule = css_rule(".time-value")
        self.assertTrue(
            readable_min_width(value_rule),
            "MS760 time input needs a >=120px / >=12ch minimum width so "
            "`e.g. 49.001` is never clipped",
        )

    def test_time_search_wraps_and_stays_inside_the_toolbar_at_any_width(self):
        toolbar_rule = css_rule(".toolbar")
        self.assertRegex(toolbar_rule, r"flex-wrap\s*:\s*wrap")

        search_rule = css_rule(".time-search")
        self.assertRegex(
            search_rule,
            r"(?:flex-wrap\s*:\s*wrap|grid-template-columns\s*:)",
            "The query controls must wrap or use a responsive grid before clipping",
        )
        self.assertRegex(
            search_rule,
            r"max-width\s*:\s*100%",
            "The query form must not grow beyond the UMAP window",
        )

    def test_medium_width_gets_a_full_query_row_before_controls_are_crowded(self):
        responsive_query = re.search(
            r"@media\s*\(max-width:\s*(?P<width>\d+)px\)"
            r"[\s\S]*?\.time-search\s*\{(?P<body>[^}]*)\}",
            UMAP_HTML,
        )
        self.assertIsNotNone(
            responsive_query,
            "A responsive breakpoint must move the MS760 query to its own row",
        )
        self.assertGreaterEqual(
            int(responsive_query.group("width")),
            900,
            "Waiting until phone width is too late for a desktop toolbar with a legend",
        )
        self.assertRegex(responsive_query.group("body"), r"width\s*:\s*100%")
        self.assertRegex(responsive_query.group("body"), r"flex-wrap\s*:\s*wrap")


if __name__ == "__main__":
    unittest.main()
