"""Versioned, project-owned LIF peak-detection semantics.

The historical V3 caller used one channel-wide prominence threshold.  That is
kept byte-for-byte compatible as detector version 1.  Detector version 2 keeps
those high-specificity calls as ``core`` evidence and adds a separate ``weak``
tier from local robust noise and pulse morphology.  Weak evidence is never an
automatic alignment/cell-training input; a human may still pair and accept it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any


LEGACY_LIF_PEAK_DETECTION: dict[str, Any] = {
    "detector_version": 1,
    "profile": "legacy_v3_fixed",
    "core": {"prominence_snr_min": 10.0},
    "weak": {
        "enabled": False,
        "prominence_snr_min": None,
        "template_similarity_min": None,
        "local_noise_block_sec": None,
    },
    "geometry": {
        "min_distance_sec": 0.02,
        "merge_gap_sec": 0.12,
        "min_width_sec": 0.02,
        "max_width_sec": 1.0,
    },
    "weak_usage": "disabled",
}


ADAPTIVE_LIF_PEAK_DETECTION: dict[str, Any] = {
    "detector_version": 2,
    "profile": "core_weak",
    "core": {"prominence_snr_min": 10.0},
    "weak": {
        "enabled": True,
        # This is a candidate-generation floor, not an acceptance threshold.
        # Shape agreement and manual review remain independent gates.
        "prominence_snr_min": 3.5,
        "template_similarity_min": 0.75,
        "local_noise_block_sec": 10.0,
        "min_core_template_peaks": 3,
        "min_core_rate_per_min": 0.50,
    },
    "geometry": {
        "min_distance_sec": 0.02,
        "merge_gap_sec": 0.12,
        "min_width_sec": 0.02,
        "max_width_sec": 1.0,
    },
    "weak_usage": "manual_review_only",
}


def legacy_lif_peak_detection(*, compatibility_mode: bool = False) -> dict[str, Any]:
    config = copy.deepcopy(LEGACY_LIF_PEAK_DETECTION)
    if compatibility_mode:
        config["compatibility_mode"] = "legacy_manifest_without_lif_peak_detection"
    return config


def adaptive_lif_peak_detection() -> dict[str, Any]:
    return copy.deepcopy(ADAPTIVE_LIF_PEAK_DETECTION)


def _finite_positive(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def normalize_lif_peak_detection(raw: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("lif_peak_detection must be an object")
    try:
        version = int(raw.get("detector_version"))
    except (TypeError, ValueError) as exc:
        raise ValueError("lif_peak_detection.detector_version must be 1 or 2") from exc
    if version not in {1, 2}:
        raise ValueError("lif_peak_detection.detector_version must be 1 or 2")

    defaults = legacy_lif_peak_detection() if version == 1 else adaptive_lif_peak_detection()
    core_raw = raw.get("core") if isinstance(raw.get("core"), dict) else {}
    weak_raw = raw.get("weak") if isinstance(raw.get("weak"), dict) else {}
    geometry_raw = raw.get("geometry") if isinstance(raw.get("geometry"), dict) else {}
    core_snr = _finite_positive(
        core_raw.get(
            "prominence_snr_min", defaults["core"]["prominence_snr_min"]
        ),
        "lif_peak_detection.core.prominence_snr_min",
    )
    geometry = {
        key: _finite_positive(
            geometry_raw.get(key, defaults["geometry"][key]),
            f"lif_peak_detection.geometry.{key}",
        )
        for key in (
            "min_distance_sec",
            "merge_gap_sec",
            "min_width_sec",
            "max_width_sec",
        )
    }
    if geometry["max_width_sec"] <= geometry["min_width_sec"]:
        raise ValueError(
            "lif_peak_detection.geometry.max_width_sec must exceed min_width_sec"
        )

    if version == 1:
        normalized = legacy_lif_peak_detection()
        normalized["core"]["prominence_snr_min"] = core_snr
        normalized["geometry"] = geometry
    else:
        enabled = bool(weak_raw.get("enabled", True))
        weak_snr = _finite_positive(
            weak_raw.get(
                "prominence_snr_min",
                defaults["weak"]["prominence_snr_min"],
            ),
            "lif_peak_detection.weak.prominence_snr_min",
        )
        similarity = _finite_positive(
            weak_raw.get(
                "template_similarity_min",
                defaults["weak"]["template_similarity_min"],
            ),
            "lif_peak_detection.weak.template_similarity_min",
        )
        if similarity > 1.0:
            raise ValueError(
                "lif_peak_detection.weak.template_similarity_min must be <= 1"
            )
        local_block = _finite_positive(
            weak_raw.get(
                "local_noise_block_sec",
                defaults["weak"]["local_noise_block_sec"],
            ),
            "lif_peak_detection.weak.local_noise_block_sec",
        )
        min_core_template_peaks = int(
            weak_raw.get(
                "min_core_template_peaks",
                defaults["weak"]["min_core_template_peaks"],
            )
        )
        if min_core_template_peaks < 1:
            raise ValueError(
                "lif_peak_detection.weak.min_core_template_peaks must be >= 1"
            )
        min_core_rate = _finite_positive(
            weak_raw.get(
                "min_core_rate_per_min",
                defaults["weak"]["min_core_rate_per_min"],
            ),
            "lif_peak_detection.weak.min_core_rate_per_min",
        )
        if weak_snr >= core_snr:
            raise ValueError(
                "lif_peak_detection weak prominence threshold must be below core threshold"
            )
        normalized = adaptive_lif_peak_detection()
        normalized["profile"] = "core_weak"
        normalized["core"]["prominence_snr_min"] = core_snr
        normalized["weak"] = {
            "enabled": enabled,
            "prominence_snr_min": weak_snr,
            "template_similarity_min": similarity,
            "local_noise_block_sec": local_block,
            "min_core_template_peaks": min_core_template_peaks,
            "min_core_rate_per_min": min_core_rate,
        }
        normalized["geometry"] = geometry
        normalized["weak_usage"] = "manual_review_only" if enabled else "disabled"

    compatibility_mode = str(raw.get("compatibility_mode") or "").strip()
    if compatibility_mode:
        normalized["compatibility_mode"] = compatibility_mode
    return normalized


def lif_peak_detection_hash(config: dict[str, Any]) -> str:
    normalized = normalize_lif_peak_detection(config)
    scientific = {
        key: value
        for key, value in normalized.items()
        if key != "compatibility_mode"
    }
    return hashlib.sha256(
        json.dumps(
            scientific,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
