"""Contracts for the single supported LIF detector standard.

Detector-v1 remains importable only as an offline scientific benchmark.  The
application, project manifest, and preprocessing protocol must all require the
adaptive detector-v2 configuration explicitly; old projects are never
silently reinterpreted or rewritten.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from annotation_app import app as app_module
from annotation_app.app import (
    APP_VERSION,
    AppData,
    BadRequest,
    HTML,
    ProjectPaths,
    lif_peak_detection_from_manifest,
    read_project_manifest,
    write_project_manifest,
)
from scripts.v3 import project_protocol
from scripts.v3 import run_v3_01_lif_trace_physical_qc as lif_qc
from scripts.v3 import run_v3_02_ms_event_calling as ms_qc
from scripts.v3.lif_peak_detection import (
    adaptive_lif_peak_detection,
    legacy_lif_peak_detection,
    lif_peak_detection_hash,
)
from tests.test_adversarial_scientific_validation import synthetic_lif_trace
from tests.test_protocol_regressions import create_legacy_project


def tree_snapshot(root: Path) -> dict[str, tuple[int, int, str] | str]:
    snapshot: dict[str, tuple[int, int, str] | str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot[relative] = "directory"
        else:
            payload = path.read_bytes()
            stat = path.stat()
            snapshot[relative] = (
                int(stat.st_size),
                int(stat.st_mtime_ns),
                hashlib.sha256(payload).hexdigest(),
            )
    return snapshot


def protocol_payload(*, detector: dict | None) -> dict:
    payload = {
        "schema_version": 4,
        "calibration_protocol": {
            "segments": [
                {
                    "segment_id": "reference",
                    "order": 1,
                    "population_label": "reference",
                    "start_min": 1.0,
                    "end_min": 2.0,
                    "reference_channels": ["G1"],
                    "boundaries_confirmed": False,
                }
            ]
        },
        "post_qc_strategy": {"mode": "disabled"},
        "annotation_config": {"annotation_start_min": 24.0},
    }
    if detector is not None:
        payload["lif_peak_detection"] = copy.deepcopy(detector)
    return payload


class DetectorV2OnlyManifestContractTest(unittest.TestCase):
    def test_manifest_without_detector_is_rejected_without_mutation(self):
        manifest = {
            "project_schema_version": 3,
            "created_by_app_version": "lma_studio_v0.3.0",
        }
        before = copy.deepcopy(manifest)

        with self.assertRaisesRegex(BadRequest, "V3|v2|重.*预处理|重新创建"):
            lif_peak_detection_from_manifest(manifest)

        self.assertEqual(manifest, before)

    def test_explicit_detector_v1_manifest_is_rejected(self):
        detector = legacy_lif_peak_detection()
        manifest = {
            "lif_peak_detection": detector,
            "lif_peak_detection_hash": lif_peak_detection_hash(detector),
        }

        with self.assertRaisesRegex(BadRequest, "V3|v1|v2|重.*预处理"):
            lif_peak_detection_from_manifest(manifest)

    def test_incomplete_or_weak_disabled_current_manifest_is_rejected(self):
        incomplete = {"detector_version": 2}
        normalized_incomplete = copy.deepcopy(adaptive_lif_peak_detection())
        disabled = adaptive_lif_peak_detection()
        disabled["weak"]["enabled"] = False
        disabled["weak_usage"] = "disabled"
        cases = (
            (
                incomplete,
                lif_peak_detection_hash(normalized_incomplete),
                "不完整",
            ),
            (disabled, lif_peak_detection_hash(disabled), "旧峰识别|自适应双层"),
        )
        for config, declared_hash, message in cases:
            with self.subTest(config=config), self.assertRaisesRegex(
                BadRequest,
                message,
            ):
                lif_peak_detection_from_manifest(
                    {
                        "lif_peak_detection": config,
                        "lif_peak_detection_hash": declared_hash,
                    }
                )

    def test_old_detector_project_load_is_a_complete_read_only_failure(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            project_dir, db_path = create_legacy_project(Path(tmp))
            manifest_path = project_dir / "lifms_project.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("lif_peak_detection", None)
            manifest.pop("lif_peak_detection_hash", None)
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            before = tree_snapshot(project_dir)

            with self.assertRaisesRegex(BadRequest, "旧峰识别|新的空目录|重跑"):
                AppData.load(
                    ProjectPaths.from_args(
                        project_dir=str(project_dir),
                        annotation_db=str(db_path),
                    )
                )

            after = tree_snapshot(project_dir)

        self.assertEqual(after, before)

    def test_manifest_writer_defaults_to_detector_v2(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            write_project_manifest(
                project_dir=root,
                raw_input_mode="external_reference",
                raw_inputs={},
                channel_identity_prior={"G2": "Day0", "R1": "Day9", "R2": "Day3"},
            )
            manifest = read_project_manifest(root)

        self.assertEqual(manifest["lif_peak_detection"], adaptive_lif_peak_detection())
        self.assertEqual(
            manifest["lif_peak_detection_hash"],
            lif_peak_detection_hash(adaptive_lif_peak_detection()),
        )

    def test_manifest_writer_rejects_detector_v1_without_writing_manifest(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp) / "new-project"
            with self.assertRaisesRegex(BadRequest, "旧峰识别|自适应双层|重新"):
                write_project_manifest(
                    project_dir=root,
                    raw_input_mode="external_reference",
                    raw_inputs={},
                    channel_identity_prior={"G2": "Day0", "R1": "Day9", "R2": "Day3"},
                    lif_peak_detection=legacy_lif_peak_detection(),
                )
            self.assertFalse(root.exists())

    def test_direct_project_creation_rejects_detector_v1_before_staging(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            parent = Path(tmp)
            target = parent / "retired-detector-project"
            with self.assertRaisesRegex(BadRequest, "旧峰识别|自适应双层"):
                AppData.create_project_from_raw_inputs(
                    project_dir=target,
                    ms_path=parent / "missing-ms.txt",
                    lif_inputs=[],
                    lif_peak_detection=legacy_lif_peak_detection(),
                )

            self.assertFalse(target.exists())
            self.assertEqual(list(parent.glob(".*.lma-building-*")), [])


class DetectorV2OnlyPreprocessingContractTest(unittest.TestCase):
    def test_missing_preprocessing_protocol_fails_without_writing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            before = sorted(path.relative_to(root) for path in root.rglob("*"))

            with self.assertRaisesRegex(ValueError, "protocol|V3|v2|重新"):
                project_protocol.load_project_protocol(root)

            after = sorted(path.relative_to(root) for path in root.rglob("*"))
        self.assertEqual(after, before)

    def test_protocol_without_detector_is_rejected(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            path = root / project_protocol.PROTOCOL_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(protocol_payload(detector=None)), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "lif_peak_detection|V3|v2"):
                project_protocol.load_project_protocol(root)

    def test_protocol_with_detector_v1_is_rejected(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            path = root / project_protocol.PROTOCOL_RELATIVE_PATH
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(protocol_payload(detector=legacy_lif_peak_detection())),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "旧峰识别|自适应双层|重新"):
                project_protocol.load_project_protocol(root)

    def test_low_level_default_is_adaptive_v2_not_v1(self):
        trace = synthetic_lif_trace(
            31415,
            channel="G1",
            detector="green",
            injections=(
                (60.0, 15.0, 0.12),
                (120.0, 15.0, 0.12),
                (180.0, 15.0, 0.12),
                (240.0, 6.0, 0.12),
            ),
        )

        corrected, meta = lif_qc.add_baseline_and_noise(trace)
        peaks = lif_qc.call_raw_peaks(corrected, meta)

        self.assertEqual(meta["detector_version"], 2)
        self.assertEqual(meta["weak_usage"], "manual_review_only")
        self.assertGreater(len(peaks), 0)
        self.assertEqual(set(peaks["detector_version"]), {2})
        self.assertTrue(set(peaks["peak_tier"]).issubset({"core", "weak"}))

    def test_both_preprocessing_runners_reject_missing_protocol_before_outputs(self):
        original_lif_root = lif_qc.ROOT
        original_ms_root = ms_qc.ROOT
        try:
            for runner in (lif_qc.run, ms_qc.run):
                with self.subTest(runner=runner.__module__), tempfile.TemporaryDirectory(
                    ignore_cleanup_errors=True
                ) as tmp:
                    root = Path(tmp)
                    with self.assertRaisesRegex(ValueError, "protocol|V3|v2"):
                        runner(root)
                    self.assertEqual(list(root.iterdir()), [])
        finally:
            lif_qc.configure_project_root(
                original_lif_root,
                allow_unbound_module_default=True,
            )
            ms_qc.configure_project_root(
                original_ms_root,
                allow_unbound_module_default=True,
            )

    def test_both_preprocessing_cli_defaults_require_protocol_before_outputs(self):
        for module in (lif_qc, ms_qc):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory(
                ignore_cleanup_errors=True
            ) as tmp, tempfile.TemporaryDirectory(
                prefix="lma-mpl-cache-",
                ignore_cleanup_errors=True,
            ) as mpl_tmp:
                root = Path(tmp)
                env = os.environ.copy()
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                env["MPLCONFIGDIR"] = mpl_tmp
                result = subprocess.run(
                    [sys.executable, str(Path(module.__file__).resolve())],
                    cwd=root,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=120,
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    list(root.iterdir()),
                    [],
                    msg=(
                        f"{module.__name__} wrote into a directory with no bound "
                        f"project protocol. Output:\n{result.stdout}"
                    ),
                )


class DetectorV2OnlyUiContractTest(unittest.TestCase):
    def test_candidate_version_and_new_project_ui_expose_one_standard(self):
        self.assertEqual(APP_VERSION, "lma_studio_v0.4.0-rc3")
        visible_markup = HTML.split("</style>", 1)[1].split("<script>", 1)[0]
        self.assertNotIn('id="importLifPeakDetectorVersion"', HTML)
        self.assertNotRegex(HTML, r"detector_version\s*:\s*1")
        self.assertNotIn("legacy_v3_fixed", HTML)
        self.assertIn('id="importLifPeakDetectorStandard"', HTML)
        self.assertNotRegex(
            visible_markup,
            r">[^<]*(?:\bv1\b|\bv2\b|detector|配置 hash|检测器版本)[^<]*<",
        )
        self.assertIn("自适应双层峰识别", visible_markup)
        self.assertRegex(visible_markup, r"高置信峰[^<]{0,180}自动")
        self.assertRegex(visible_markup, r"弱候选峰[^<]{0,180}人工[^<]{0,180}不参与自动")

    def test_visible_ui_uses_user_language_instead_of_internal_policy_tokens(self):
        visible_markup = HTML.split("</style>", 1)[1].split("<script>", 1)[0]
        visible_text = re.sub(r"<[^>]+>", " ", visible_markup)
        visible_text = re.sub(r"\s+", " ", visible_text)

        for internal_token in (
            "Green-only",
            "Red-only",
            "Red+Green",
            "calibration_protocol",
            "scheduled_windows",
            "disabled",
            "无标签 delta",
            "base shift",
            "green_axis",
            "red_axis",
        ):
            self.assertNotIn(internal_token, visible_text)

        for user_facing_phrase in (
            "仅绿色通道",
            "仅红色通道",
            "红绿联合",
            "自动估计时间差的范围",
            "Off",
            "QC signature",
            "Scheduled windows",
        ):
            self.assertIn(user_facing_phrase, visible_text)
        self.assertIn("QC 时间未知或不规律", HTML)
        self.assertIn("QC 时间已知", HTML)

        for raw_runtime_message in (
            "后段 QC=${postQcMode}",
            "后段 QC 策略为 disabled",
            "旧项目 v0.3 协议",
            "base shift:",
        ):
            self.assertNotIn(raw_runtime_message, HTML)

    def test_api_errors_are_translated_before_the_ui_displays_them(self):
        translator = getattr(app_module, "user_facing_error_message", None)
        self.assertIsNotNone(
            translator,
            "API errors need one user-facing translation boundary before alerts render",
        )

        raw_messages = (
            "项目峰识别配置无效：lif_peak_detection.detector_version must be 1 or 2",
            "calibration_protocol 未覆盖 green_axis",
            "post_qc_strategy.mode 必须是 signature、scheduled_windows 或 disabled",
            "preview_hash no longer matches draft time model; recalculate delta",
            "lif_anchor_peak_ids cannot train a frozen time-axis model",
        )
        forbidden = re.compile(
            r"(?i)(?:\bv(?:0\.3|1|2)\b|detector|hash|calibration_protocol|"
            r"post_qc_strategy|qc_anchor_channels|signature|scheduled_windows|"
            r"disabled|green_axis|red_axis|preview_hash|anchor|delta|frozen|draft|"
            r"time[-_]axis|peak_tier)"
        )
        for raw in raw_messages:
            with self.subTest(raw=raw):
                translated = translator(raw)
                self.assertTrue(translated.strip())
                self.assertIsNone(forbidden.search(translated), translated)

        self.assertEqual(
            translator("start_min, window_min, and preview_ms_delta_sec must be numeric"),
            "开始时间、窗口宽度和预览 MS 时间差必须填写为数字。",
        )
        self.assertEqual(
            translator("annotation_start_min must be numeric"),
            "事件标注起点必须填写为数字。",
        )

        self.assertIn("user_facing_error_message", HTML)
        self.assertRegex(
            HTML,
            r"parsed\.error\)\s*return\s+user_facing_error_message\(parsed\.error\)",
        )

    def test_missing_population_label_never_exposes_internal_segment_id(self):
        self.assertIn("function calibrationSegmentDisplayName(segment)", HTML)
        self.assertNotIn("segment.population_label || segment.segment_id", HTML)
        self.assertRegex(
            HTML,
            r"calibrationSegmentDisplayName\(segment\).*参考段",
        )

    def test_legacy_detector_remains_available_only_for_offline_benchmarks(self):
        legacy = legacy_lif_peak_detection()
        self.assertEqual(legacy["detector_version"], 1)
        self.assertFalse(legacy["weak"]["enabled"])


if __name__ == "__main__":
    unittest.main()
