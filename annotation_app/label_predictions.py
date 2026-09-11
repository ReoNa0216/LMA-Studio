"""Independent, evidence-bound label predictions; never writes AnnotationStore.

The operator exports evidence on a project copy. A predictor receives only that
export, not the normal window response or access to the annotation project.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
from datetime import datetime, timezone

SCHEMA = "lma-label-evidence-v1"
RESULT_SCHEMA = "lma-label-predictions-v1"
PROMPT_VERSION = "label-correct-visual-v1"
PROMPT = """Read the actual LIF and MS760 waveforms and the stable candidate IDs.
For every target MS event return ms_event_id, event_version, label,
lif_candidate_ids, status (certain or uncertain), and reason. Use only the
supplied channel labels, time/order and waveform evidence. Multiple candidates
of one label may support that label; a unique pair is unnecessary. Conflicting
labels or inadequate evidence require status=uncertain and label=null. Candidate
IDs are evidence, never accepted pairs. Do not use barcode, prior annotations,
other methods, full spectra or UMAP. Return one row per target, including uncertain.
"""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def project_binding(app):
    from annotation_app.app import clean_value
    model = app.active_time_model()
    if model.get("status") != "frozen":
        raise ValueError("需要已冻结的时间模型；请在项目副本上完成校准。")
    manifest = app.manifest
    # No historical cell decisions or model fit payload leaves the operator.
    identity = {
        "dataset_id": manifest.get("dataset_id"),
        "tables": manifest["intermediate_tables"],
        "event_map": manifest.get("cell_event_map"),
        "ms_import": manifest.get("ms_event_package"),
        "model": model,
        "alignment": app.alignment,
        "layout": app.acquisition_layout,
    }
    return digest(clean_value(identity))


def build_evidence(app, start_min, window_min, *, context_sec=2.0):
    from annotation_app.app import clean_value
    if not all(math.isfinite(float(v)) for v in (start_min, window_min, context_sec)):
        raise ValueError("窗口参数必须有限")
    if window_min < .25 or window_min > 10 or context_sec < 0 or context_sec > 30:
        raise ValueError("窗口范围 0.25–10 min，上下文 0–30 sec")
    binding = project_binding(app)
    end_min = start_min + window_min
    if start_min < float(app.active_time_model()["annotation_start_min"]):
        raise ValueError("预测窗口必须位于事件标注阶段")
    window = app.window(start_min - context_sec / 60,
                        window_min + context_sec / 30, lif_signal_mode="raw")
    if window["start_min"] > start_min or window["end_min"] < end_min:
        raise ValueError("请求窗口超出可用时间范围")
    channels = {r["channel"]: r.get("identity_prior") or r["channel"]
                for r in app.acquisition_layout["lif_channels"]
                if r.get("use_for_cell_annotation", True)}
    peaks = [{"lif_candidate_id": p["peak_id"], "channel": p["channel"],
              "label": channels[p["channel"]], "time_min": p["plot_time_min"],
              "raw_time_min": p["raw_time_min"], "height": p["display_y"]}
             for p in window["lif_peaks"] if p["channel"] in channels]
    events = []
    table_hash = digest(app.manifest["intermediate_tables"]["ms_events"])
    for e in window["ms_events"]:
        # Legacy sequential IDs are bound to the exact saved table, never
        # presented as upstream v2 revisions that the old project did not have.
        version = (f"revision:{e['revision']}:{table_hash}" if e.get("revision") is not None
                   else f"legacy-table:{table_hash}")
        events.append({"ms_event_id": e["event_id"], "event_version": version,
                       "time_min": e["plot_time_min"], "raw_time_min": e["raw_time_min"],
                       "height": e["pc34_760_apex"],
                       "target": bool(e["in_cell_event_map"] and start_min <= e["plot_time_min"] < end_min)})
    evidence = {"schema": SCHEMA, "binding": binding, "prompt_version": PROMPT_VERSION,
                "start_min": start_min, "window_min": window_min, "context_sec": context_sec,
                "time_model_version": app.active_time_model()["time_model_version"],
                "channels": channels, "lif_candidates": peaks, "ms_events": events,
                "traces": {**{c: window["lif_traces"][c] for c in channels},
                           "MS760": window["ms_traces"]["pc34_760_linear"]}}
    evidence = clean_value(evidence)
    lo, hi = start_min - context_sec / 60, end_min + context_sec / 60
    evidence["traces"] = {name: [[p["x"], p["y"]] for p in points if lo <= p["x"] <= hi]
                          for name, points in evidence["traces"].items()}
    evidence["lif_candidates"] = [p for p in peaks if lo <= p["time_min"] <= hi]
    evidence["ms_events"] = [e for e in events if lo <= e["time_min"] <= hi]
    if project_binding(app) != binding:
        raise ValueError("读取时项目时间模型改变，请重新导出")
    evidence["evidence_id"] = digest(evidence)
    return evidence


def validate_rows(evidence, rows):
    targets = {r["ms_event_id"]: r for r in evidence["ms_events"] if r["target"]}
    candidates = {r["lif_candidate_id"]: r for r in evidence["lif_candidates"]}
    if not isinstance(rows, list):
        raise ValueError("predictions 必须为列表")
    seen = set()
    for row in rows:
        required = {"ms_event_id", "event_version", "label", "lif_candidate_ids", "status", "reason"}
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError("预测字段不完整或包含不允许的字段")
        event_id = row["ms_event_id"]
        if not isinstance(event_id, str) or event_id not in targets or event_id in seen:
            raise ValueError("窗口外、未知或重复 MS ID")
        seen.add(event_id)
        if row["event_version"] != targets[event_id]["event_version"]:
            raise ValueError("MS 事件版本不一致")
        ids = row["lif_candidate_ids"]
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("候选 ID 必须为字符串列表")
        if len(ids) != len(set(ids)) or any(i not in candidates for i in ids):
            raise ValueError("未知或重复 LIF 候选 ID")
        if row["status"] == "certain":
            if not ids or row["label"] not in evidence["channels"].values():
                raise ValueError("确定标签需要合法类别及候选证据")
            if any(candidates[i]["label"] != row["label"] for i in ids):
                raise ValueError("跨类别候选不能保存为确定标签")
        elif row["status"] != "uncertain" or row["label"] is not None:
            raise ValueError("不确定判断必须使用 uncertain 与 null 标签")
        if not isinstance(row["reason"], str) or not row["reason"].strip() or len(row["reason"]) > 2000:
            raise ValueError("需要简短可核对的判断依据")
    if seen != set(targets):
        raise ValueError("必须覆盖窗口的全部目标事件；无结论时明确 uncertain")
    return rows


def result_directory(app):
    return app.project.annotation_db_path.parent / "label_predictions"


def safe_run_id(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id):
        raise ValueError("运行 ID 只能包含字母、数字、下划线、连字符")
    return run_id


def save_batch(app, evidence, batch):
    """Validate against current data, then atomically publish a separate run file."""
    current = build_evidence(app, evidence["start_min"], evidence["window_min"],
                             context_sec=evidence["context_sec"])
    if evidence != current or batch.get("evidence_id") != current["evidence_id"]:
        raise ValueError("证据、来源或时间模型已改变；请重新导出")
    run_id = safe_run_id(batch.get("run_id"))
    method = batch.get("method")
    if not isinstance(method, dict) or set(method) != {"route", "model", "prompt_version", "started_at", "elapsed_seconds"}:
        raise ValueError("需要完整的方法、模型、prompt 与耗时记录")
    if method["route"] not in {"codex_visual", "physical_algorithm", "independent_human"}:
        raise ValueError("未知比较路线")
    if method["prompt_version"] != PROMPT_VERSION or not isinstance(method["model"], str) or not method["model"]:
        raise ValueError("方法版本记录缺失")
    if not isinstance(method["elapsed_seconds"], (float, int)) or not math.isfinite(method["elapsed_seconds"]) or method["elapsed_seconds"] < 0:
        raise ValueError("耗时必须为非负有限数")
    datetime.fromisoformat(method["started_at"])
    rows = validate_rows(evidence, batch.get("predictions"))
    folder = result_directory(app)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run_id}.json"
    lock = folder / f"{run_id}.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    tmp = folder / f"{run_id}.partial"
    try:
        result = read_json(path) if path.exists() else {
            "schema": RESULT_SCHEMA, "run_id": run_id, "binding": evidence["binding"],
            "source": "independent_prediction", "review_status": "unreviewed_prediction",
            "method": {k: v for k, v in method.items() if k != "elapsed_seconds"},
            "windows": [], "predictions": []}
        if result["binding"] != evidence["binding"] or result["method"] != {k: v for k, v in method.items() if k != "elapsed_seconds"}:
            raise ValueError("同一运行不能混用来源、时间模型或方法版本")
        by_id = {r["ms_event_id"]: r for r in result["predictions"]}
        for row in rows:
            if row["ms_event_id"] in by_id and by_id[row["ms_event_id"]] != row:
                raise ValueError("重叠窗口预测冲突；保留已完成结果，请使用新运行 ID")
            by_id[row["ms_event_id"]] = row
        if evidence["evidence_id"] in [w["evidence_id"] for w in result["windows"]]:
            return path
        result["predictions"] = list(by_id.values())
        result["windows"].append({"evidence_id": evidence["evidence_id"], "evidence": evidence,
                                   "elapsed_seconds": method["elapsed_seconds"]})
        result["saved_at"] = datetime.now(timezone.utc).isoformat()
        write_new(tmp, result)
        os.replace(tmp, path)
        return path
    finally:
        os.close(fd)
        lock.unlink()


def render_svg(evidence, rows=()):
    """Actual sampled values, separate linear axes; dashed support is not pairing."""
    esc = lambda x: html.escape(str(x), quote=True)
    traces = evidence["traces"]
    left, width, height = 155, 1110, 175
    lo = evidence["start_min"] - evidence["context_sec"] / 60
    hi = evidence["start_min"] + evidence["window_min"] + evidence["context_sec"] / 60
    x = lambda t: left + (t - lo) / (hi - lo) * width
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1320 {len(traces)*height+55}" role="img" aria-label="真实波形与稳定事件 ID">', '<rect width="100%" height="100%" fill="white"/>']
    positions = {}
    for index, (name, points) in enumerate(traces.items()):
        top = 30 + height * index
        values = [p[1] for p in points if p[1] is not None]
        ymin, ymax = (min(values), max(values)) if values else (0, 1)
        span = max(ymax - ymin, 1e-12)
        y = lambda v: top + 135 - (v - ymin) / span * 95
        color = "#34465d" if name == "MS760" else ["#18784c", "#aa3152", "#6e50ad", "#b47315"][index % 4]
        out.append(f'<text x="8" y="{top+30}" font-size="17" fill="{color}">{esc(name)}</text><text x="8" y="{top+55}" font-size="12">{esc(evidence["channels"].get(name,""))}</text><text x="8" y="{top+80}" font-size="11">max {ymax:.3g}</text>')
        coords = " ".join(f"{x(p[0]):.2f},{y(p[1]):.2f}" for p in points if p[1] is not None)
        out.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="1.1"/>')
        events = evidence["ms_events"] if name == "MS760" else [p for p in evidence["lif_candidates"] if p["channel"] == name]
        for j, event in enumerate(events):
            eid = event.get("ms_event_id", event.get("lif_candidate_id"))
            px = x(event["time_min"])
            py = y(event["height"])
            positions[eid] = (px, py)
            target = event.get("target", True)
            label = eid.replace('MS_pc34_primary_', 'MS:').replace('_merged_', ':')
            out.append(f'<circle cx="{px:.2f}" cy="{py:.2f}" r="3" fill="{color}"/><text x="{px:.2f}" y="{top+10+(j%3)*15}" text-anchor="middle" font-size="10" fill="{color if target else "#777"}"><title>{esc(eid)}</title>{esc(label)}</text>')
        out.append(f'<line x1="{left}" y1="{top+145}" x2="{left+width}" y2="{top+145}" stroke="#bbb"/>')
    for row in rows:
        if row["ms_event_id"] not in positions:
            continue
        a = positions[row["ms_event_id"]]
        for candidate in row["lif_candidate_ids"]:
            if candidate in positions:
                b = positions[candidate]
                out.append(f'<line x1="{a[0]:.2f}" y1="{a[1]:.2f}" x2="{b[0]:.2f}" y2="{b[1]:.2f}" stroke="#b56d15" stroke-opacity=".6" stroke-dasharray="5 5"><title>预测支持证据，未经人工审核</title></line>')
    bottom = height * len(traces) + 30
    for i in range(7):
        t = lo + (hi-lo)*i/6
        out.append(f'<text x="{x(t):.2f}" y="{bottom}" text-anchor="middle" font-size="13">{t:.4f}</text>')
    out.append('</svg>')
    return "".join(out)


def render_png(evidence, path):
    """Optional development renderer; Matplotlib is not a desktop dependency."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(len(evidence["traces"]), 1, figsize=(18, 9), sharex=True)
    for ax, (name, points) in zip(axes, evidence["traces"].items()):
        ax.plot([p[0] for p in points], [p[1] for p in points], linewidth=.9)
        events = evidence["ms_events"] if name == "MS760" else [p for p in evidence["lif_candidates"] if p["channel"] == name]
        for j, event in enumerate(events):
            eid = event.get("ms_event_id", event.get("lif_candidate_id"))
            label = eid.replace('MS_pc34_primary_', 'MS:').replace('_merged_', ':')
            ax.axvline(event["time_min"], color="#b56d15", alpha=.35, linewidth=.8)
            ax.text(event["time_min"], 1.02 + .065*(j%3), label, transform=ax.get_xaxis_transform(), fontsize=8, ha="center")
        ax.set_ylabel(name + "\nraw intensity")
        ax.grid(alpha=.2)
    axes[-1].set_xlabel("Aligned acquisition time (min); IDs shortened only by replacing fixed prefixes")
    axes[-1].set_xlim(evidence["start_min"]-evidence["context_sec"]/60,
                      evidence["start_min"]+evidence["window_min"]+evidence["context_sec"]/60)
    fig.suptitle("LMA measured waveforms | independent prediction input | " + evidence["evidence_id"][:16])
    fig.tight_layout(h_pad=3)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def review_html(result):
    esc = lambda x: html.escape(str(x), quote=True)
    rows = result["predictions"]
    chunks = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>LMA 独立标签预测</title><style>body{font:16px system-ui;margin:32px;color:#203249;background:#f4f6f9}main{max-width:1400px;margin:auto}svg{width:100%;background:white}table{border-collapse:collapse;width:100%;background:white}td,th{padding:10px;border-bottom:1px solid #dde2e8;text-align:left}h1{font-size:25px}code{overflow-wrap:anywhere}p{line-height:1.6}</style><main><h1>独立标签预测 · 未经人工审核</h1>',
              f'<p>运行：{esc(result["run_id"])} · 路线：{esc(result["method"]["route"])} · 模型：{esc(result["method"]["model"])}<br>虚线表示候选支持证据。结果不进入人工 accepted 或科学标签导出。</p>']
    for window in result["windows"]:
        chunks.append(render_svg(window["evidence"], rows))
    chunks.append('<table><tr><th>MS 稳定 ID</th><th>预测类别</th><th>状态</th><th>LIF 候选 ID</th><th>依据</th></tr>')
    for row in rows:
        chunks.append('<tr>'+''.join(f'<td>{esc(v)}</td>' for v in (row["ms_event_id"], row["label"] or '待判断', row["status"], ', '.join(row["lif_candidate_ids"]), row["reason"]))+'</tr>')
    return ''.join(chunks)+'</table></main></html>'


def current_review(app, run_id):
    result = read_json(result_directory(app) / f"{safe_run_id(run_id)}.json")
    if result["binding"] != project_binding(app):
        raise ValueError("预测对应旧事件或时间模型，不能作为当前结果展示")
    seen = set()
    if len({r["ms_event_id"] for r in result["predictions"]}) != len(result["predictions"]):
        raise ValueError("预测结果包含重复 MS ID")
    for window in result["windows"]:
        e = window["evidence"]
        current = build_evidence(app, e["start_min"], e["window_min"], context_sec=e["context_sec"])
        if current != e:
            raise ValueError("预测输入证据已改变")
        targets = {r["ms_event_id"] for r in e["ms_events"] if r["target"]}
        validate_rows(e, [r for r in result["predictions"] if r["ms_event_id"] in targets])
        seen.update(targets)
    if seen != {r["ms_event_id"] for r in result["predictions"]}:
        raise ValueError("预测包含没有输入窗口的事件")
    return review_html(result)
