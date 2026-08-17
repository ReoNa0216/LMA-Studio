"""Standalone high-DPI Canvas page for the project cell-event UMAP."""

from annotation_app.visual_palette import signal_palette_json

UMAP_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>LMA Studio · UMAP</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #172033;
      --muted: #667085;
      --line: #d8dee9;
      --surface: #ffffff;
      --canvas: #f7f8fa;
      font-family: Inter, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; color: var(--ink); }
    body { background: var(--canvas); }
    .shell { width: 100%; height: 100%; display: grid; grid-template-rows: auto 1fr; }
    .toolbar {
      min-height: 52px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 14px;
      padding: 8px 12px;
      background: rgba(255,255,255,.96);
      border-bottom: 1px solid var(--line);
      box-shadow: 0 1px 3px rgba(16,24,40,.05);
      z-index: 3;
    }
    .title { font-weight: 720; white-space: nowrap; }
    .identity {
      min-width: 0;
      color: var(--muted);
      font-size: 12px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .spacer { flex: 1; }
    .time-search {
      display: inline-flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 5px;
      min-width: 0;
      max-width: 100%;
      white-space: nowrap;
    }
    .time-search label { font-size: 12px; font-weight: 650; }
    .time-search input {
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 0 8px;
      color: var(--ink);
      background: var(--surface);
      font: inherit;
    }
    .time-search input:focus { outline: 2px solid rgba(47,111,237,.18); border-color: #2f6fed; }
    .time-value { width: 9rem; min-width: 12ch; }
    .time-tolerance { width: 5.5rem; min-width: 8ch; }
    .time-status { min-width: 0; max-width: 150px; overflow: hidden; text-overflow: ellipsis; color: var(--muted); font-size: 11px; }
    .compact-button { width: auto; padding: 0 9px; font-size: 12px; font-weight: 650; white-space: nowrap; }
    .legend { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px 12px; font-size: 12px; }
    .legend-item { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
    .swatch { width: 9px; height: 9px; border-radius: 50%; border: 1px solid rgba(0,0,0,.12); }
    button {
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--surface);
      color: var(--ink);
      cursor: pointer;
    }
    button:hover { border-color: #98a2b3; background: #f9fafb; }
    .fit-button {
      width: auto;
      padding: 0 11px;
      font-size: 12px;
      font-weight: 650;
      white-space: nowrap;
    }
    .plot { min-height: 0; position: relative; }
    canvas { display: block; width: 100%; height: 100%; cursor: grab; touch-action: none; }
    canvas.dragging { cursor: grabbing; }
    .gesture-hint {
      position: absolute;
      right: 16px;
      bottom: 12px;
      padding: 5px 8px;
      border: 1px solid rgba(152,162,179,.35);
      border-radius: 6px;
      background: rgba(255,255,255,.84);
      color: var(--muted);
      font-size: 11px;
      pointer-events: none;
    }
    .empty {
      position: absolute;
      inset: 0;
      display: none;
      place-items: center;
      padding: 28px;
      text-align: center;
      color: var(--muted);
      pointer-events: none;
    }
    .tooltip {
      position: absolute;
      display: none;
      max-width: min(420px, calc(100% - 24px));
      padding: 8px 10px;
      border: 1px solid rgba(17,24,39,.16);
      border-radius: 7px;
      background: rgba(255,255,255,.97);
      box-shadow: 0 8px 24px rgba(16,24,40,.14);
      font-size: 12px;
      line-height: 1.45;
      pointer-events: none;
      z-index: 4;
    }
    .tooltip strong { display: block; margin-bottom: 2px; }
    .tooltip .muted { color: var(--muted); overflow-wrap: anywhere; }
    @media (max-width: 980px) {
      .time-search { order: 2; width: 100%; flex-wrap: wrap; }
      .time-status { max-width: none; }
    }
    @media (max-width: 760px) {
      .toolbar { align-items: flex-start; flex-wrap: wrap; }
      .legend { order: 3; width: 100%; justify-content: flex-start; }
      .identity { max-width: 45vw; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="toolbar">
      <span class="title">事件 UMAP</span>
      <span id="identity" class="identity">正在读取项目…</span>
      <form id="timeSearch" class="time-search" title="按 Track 中显示的 MS760 时间定位事件；只查询，不修改标注">
        <label for="timeQuery">MS760 time (min)</label>
        <input id="timeQuery" class="time-value" type="number" step="any" inputmode="decimal" placeholder="e.g. 49.001">
        <label for="timeTolerance">±</label>
        <input id="timeTolerance" class="time-tolerance" type="number" min="0" step="0.001" value="0.001" inputmode="decimal" aria-label="MS760 time tolerance in minutes">
        <span class="identity">min</span>
        <button id="findTime" class="compact-button" type="submit">Find</button>
        <button id="clearTime" class="compact-button" type="button">Clear</button>
        <span id="timeStatus" class="time-status" aria-live="polite"></span>
      </form>
      <span class="spacer"></span>
      <div id="legend" class="legend"></div>
      <button id="fit" class="fit-button" type="button"
              title="恢复缩放和位置以显示全部事件点；不会修改任何标注"
              aria-label="显示全部事件点并重新居中，不会修改标注">显示全部点</button>
    </header>
    <section id="plot" class="plot">
      <canvas id="canvas" aria-label="单细胞事件 UMAP"></canvas>
      <div id="empty" class="empty"></div>
      <div id="tooltip" class="tooltip"></div>
      <div class="gesture-hint">滚轮缩放 · 拖动平移 · 单击定位事件</div>
    </section>
  </main>
  <script>
    const SIGNAL_COLORS = __LMA_SIGNAL_COLORS__;
    const COLORS = {
      unknown: '#98A2B3',
      qc: '#111827',
      conflict: '#ffffff',
    };
    const channel = ('BroadcastChannel' in window) ? new BroadcastChannel('lma-studio-state-v1') : null;
    const canvas = document.getElementById('canvas');
    const ctx = canvas.getContext('2d');
    const plot = document.getElementById('plot');
    const tooltip = document.getElementById('tooltip');
    const empty = document.getElementById('empty');
    const identity = document.getElementById('identity');
    const legend = document.getElementById('legend');
    const timeQuery = document.getElementById('timeQuery');
    const timeTolerance = document.getElementById('timeTolerance');
    const timeStatus = document.getElementById('timeStatus');
    let payload = null;
    let points = [];
    let revision = '';
    let projectKey = '';
    let view = { scale: 1, tx: 0, ty: 0 };
    let fitted = false;
    let dragging = false;
    let moved = false;
    let dragStart = null;
    let hoverPoint = null;
    let matchedTimeEventIds = new Set();
    let lastCanvasWidth = 0;
    let lastCanvasHeight = 0;

    const AXIS_MARGIN = {
      left: 58,
      right: 22,
      top: 20,
      bottom: 48,
    };

    function escapeText(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[char]));
    }

    function stableColor(channelName) {
      const name = String(channelName || '').toUpperCase();
      return SIGNAL_COLORS[name] || COLORS.unknown;
    }

    function pointColor(point) {
      if (point.classification === 'qc') return COLORS.qc;
      if (point.classification === 'cell') return stableColor(point.lif_channel);
      return COLORS.unknown;
    }

    function pointMs760Time(point) {
      const value = point?.scan_start_time;
      if (value === null || value === undefined || value === '') return NaN;
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : NaN;
    }

    function setEmpty(message) {
      empty.textContent = message || '';
      empty.style.display = message ? 'grid' : 'none';
    }

    function projectIdentity(data) {
      return `${data?.project_id || ''}:${data?.map_sha256 || ''}`;
    }

    function bounds(sourcePoints = points) {
      if (!sourcePoints.length) return null;
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      for (const point of sourcePoints) {
        minX = Math.min(minX, Number(point.UMAP1));
        maxX = Math.max(maxX, Number(point.UMAP1));
        minY = Math.min(minY, Number(point.UMAP2));
        maxY = Math.max(maxY, Number(point.UMAP2));
      }
      return { minX, maxX, minY, maxY };
    }

    function plotArea() {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      const left = Math.min(AXIS_MARGIN.left, Math.max(30, width * .24));
      const right = Math.max(left + 1, width - Math.min(AXIS_MARGIN.right, width * .1));
      const top = Math.min(AXIS_MARGIN.top, Math.max(8, height * .08));
      const bottom = Math.max(top + 1, height - Math.min(AXIS_MARGIN.bottom, height * .22));
      return { left, right, top, bottom, width: right - left, height: bottom - top };
    }

    function fitPointSet(sourcePoints) {
      const box = bounds(sourcePoints);
      if (!box || !canvas.clientWidth || !canvas.clientHeight) return;
      const full = bounds(points) || box;
      const area = plotArea();
      const padding = Math.min(22, Math.max(8, Math.min(area.width, area.height) * .06));
      const minimumDx = Math.max((full.maxX - full.minX) * .12, 1e-6);
      const minimumDy = Math.max((full.maxY - full.minY) * .12, 1e-6);
      const dx = Math.max(box.maxX - box.minX, minimumDx);
      const dy = Math.max(box.maxY - box.minY, minimumDy);
      const usableW = Math.max(area.width - padding * 2, 1);
      const usableH = Math.max(area.height - padding * 2, 1);
      view.scale = Math.min(usableW / dx, usableH / dy);
      view.tx = (area.left + area.right) / 2 - ((box.minX + box.maxX) / 2) * view.scale;
      view.ty = (area.top + area.bottom) / 2 + ((box.minY + box.maxY) / 2) * view.scale;
      fitted = true;
      draw();
    }

    function fitView() {
      fitPointSet(points);
    }

    function screenPoint(point) {
      return {
        x: Number(point.UMAP1) * view.scale + view.tx,
        y: -Number(point.UMAP2) * view.scale + view.ty,
      };
    }

    function niceTickStep(span, targetCount = 6) {
      const rough = Math.max(Math.abs(span), 1e-12) / Math.max(targetCount, 1);
      const magnitude = 10 ** Math.floor(Math.log10(rough));
      const normalized = rough / magnitude;
      const factor = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
      return factor * magnitude;
    }

    function tickValues(minimum, maximum, targetCount) {
      const step = niceTickStep(maximum - minimum, targetCount);
      const epsilon = step * 1e-9;
      const first = Math.ceil((minimum - epsilon) / step) * step;
      const values = [];
      for (let value = first; value <= maximum + epsilon && values.length < 100; value += step) {
        values.push(Math.abs(value) < epsilon ? 0 : value);
      }
      return { step, values };
    }

    function formatTick(value, step) {
      const absolute = Math.abs(value);
      if ((absolute > 0 && absolute < 1e-4) || absolute >= 1e5) {
        return value.toExponential(1);
      }
      const decimals = Math.max(0, Math.min(6, -Math.floor(Math.log10(Math.abs(step))) + 1));
      const formatted = value.toFixed(decimals);
      return decimals ? formatted.replace(/\.?0+$/, '') : formatted;
    }

    function drawAxes() {
      const area = plotArea();
      if (!Number.isFinite(view.scale) || view.scale <= 0 || area.width <= 1 || area.height <= 1) return;
      const xMinimum = (area.left - view.tx) / view.scale;
      const xMaximum = (area.right - view.tx) / view.scale;
      const yMinimum = (view.ty - area.bottom) / view.scale;
      const yMaximum = (view.ty - area.top) / view.scale;
      const xTicks = tickValues(xMinimum, xMaximum, Math.max(3, Math.floor(area.width / 105)));
      const yTicks = tickValues(yMinimum, yMaximum, Math.max(3, Math.floor(area.height / 75)));

      ctx.save();
      ctx.lineWidth = 1;
      ctx.font = '11px "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';

      ctx.strokeStyle = 'rgba(152,162,179,.16)';
      ctx.beginPath();
      for (const value of xTicks.values) {
        const x = value * view.scale + view.tx;
        ctx.moveTo(x, area.top);
        ctx.lineTo(x, area.bottom);
      }
      for (const value of yTicks.values) {
        const y = -value * view.scale + view.ty;
        ctx.moveTo(area.left, y);
        ctx.lineTo(area.right, y);
      }
      ctx.stroke();

      ctx.strokeStyle = '#98a2b3';
      ctx.beginPath();
      ctx.moveTo(area.left, area.top);
      ctx.lineTo(area.left, area.bottom);
      ctx.lineTo(area.right, area.bottom);
      ctx.stroke();

      ctx.fillStyle = '#667085';
      ctx.textBaseline = 'top';
      ctx.textAlign = 'center';
      for (const value of xTicks.values) {
        const x = value * view.scale + view.tx;
        ctx.beginPath();
        ctx.moveTo(x, area.bottom);
        ctx.lineTo(x, area.bottom + 4);
        ctx.stroke();
        ctx.fillText(formatTick(value, xTicks.step), x, area.bottom + 7);
      }

      ctx.textBaseline = 'middle';
      ctx.textAlign = 'right';
      for (const value of yTicks.values) {
        const y = -value * view.scale + view.ty;
        ctx.beginPath();
        ctx.moveTo(area.left - 4, y);
        ctx.lineTo(area.left, y);
        ctx.stroke();
        ctx.fillText(formatTick(value, yTicks.step), area.left - 7, y);
      }

      ctx.fillStyle = '#344054';
      ctx.font = '600 12px "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'bottom';
      ctx.fillText('UMAP1', (area.left + area.right) / 2, canvas.clientHeight - 5);
      ctx.save();
      ctx.translate(14, (area.top + area.bottom) / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.fillText('UMAP2', 0, 0);
      ctx.restore();
      ctx.restore();
    }

    function resizeCanvas() {
      const ratio = Math.max(1, window.devicePixelRatio || 1);
      const cssWidth = Math.max(1, canvas.clientWidth);
      const cssHeight = Math.max(1, canvas.clientHeight);
      const sizeChanged = cssWidth !== lastCanvasWidth || cssHeight !== lastCanvasHeight;
      lastCanvasWidth = cssWidth;
      lastCanvasHeight = cssHeight;
      const width = Math.max(1, Math.round(cssWidth * ratio));
      const height = Math.max(1, Math.round(cssHeight * ratio));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      if (points.length && (sizeChanged || !fitted)) fitView();
      else draw();
    }

    function draw() {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      ctx.clearRect(0, 0, width, height);
      if (!points.length) return;
      drawAxes();
      const area = plotArea();
      const radius = Math.max(2.2, Math.min(4.2, 2.6 + Math.log10(Math.max(view.scale, 1)) * .35));
      ctx.save();
      ctx.beginPath();
      ctx.rect(area.left, area.top, area.width, area.height);
      ctx.clip();
      for (const point of points) {
        const screen = screenPoint(point);
        if (
          screen.x < area.left - 10 || screen.y < area.top - 10
          || screen.x > area.right + 10 || screen.y > area.bottom + 10
        ) continue;
        ctx.beginPath();
        ctx.arc(screen.x, screen.y, radius, 0, Math.PI * 2);
        ctx.fillStyle = pointColor(point);
        ctx.fill();
        if (point.classification === 'conflict') {
          ctx.lineWidth = 2;
          ctx.strokeStyle = '#d92d20';
          ctx.stroke();
          ctx.beginPath();
          ctx.moveTo(screen.x - radius * .7, screen.y - radius * .7);
          ctx.lineTo(screen.x + radius * .7, screen.y + radius * .7);
          ctx.moveTo(screen.x + radius * .7, screen.y - radius * .7);
          ctx.lineTo(screen.x - radius * .7, screen.y + radius * .7);
          ctx.stroke();
        } else if (point === hoverPoint) {
          ctx.lineWidth = 1.7;
          ctx.strokeStyle = '#ffffff';
          ctx.stroke();
          ctx.beginPath();
          ctx.arc(screen.x, screen.y, radius + 2, 0, Math.PI * 2);
          ctx.lineWidth = 1;
          ctx.strokeStyle = '#344054';
          ctx.stroke();
        }
        if (matchedTimeEventIds.has(String(point.ms_event_id))) {
          ctx.beginPath();
          ctx.arc(screen.x, screen.y, radius + 4, 0, Math.PI * 2);
          ctx.lineWidth = 2.4;
          ctx.strokeStyle = '#d92d20';
          ctx.stroke();
        }
      }
      ctx.restore();
    }

    function statusText(point) {
      if (point.classification === 'qc') return 'QC';
      if (point.classification === 'cell') {
        const channelName = String(point.lif_channel || '').trim();
        const label = String(point.label || '').trim();
        if (channelName && label.toLowerCase().startsWith(channelName.toLowerCase())) {
          return label;
        }
        return [channelName, label].filter(Boolean).join(' ') || '细胞';
      }
      if (point.classification === 'conflict') return '标注冲突';
      return '未标注';
    }

    function nearestPoint(x, y) {
      const area = plotArea();
      if (x < area.left || x > area.right || y < area.top || y > area.bottom) return null;
      let closest = null;
      let distance2 = 90;
      for (const point of points) {
        const screen = screenPoint(point);
        const dx = screen.x - x;
        const dy = screen.y - y;
        const candidate = dx * dx + dy * dy;
        if (candidate < distance2) {
          distance2 = candidate;
          closest = point;
        }
      }
      return closest;
    }

    function showTooltip(event, point) {
      hoverPoint = point;
      if (!point) {
        tooltip.style.display = 'none';
        draw();
        return;
      }
      tooltip.innerHTML = `
        <strong>MS760 时间：${Number(point.scan_start_time).toFixed(6)} min</strong>
        <div>${escapeText(statusText(point))}</div>`;
      tooltip.style.display = 'block';
      const margin = 12;
      const maxLeft = plot.clientWidth - tooltip.offsetWidth - margin;
      const maxTop = plot.clientHeight - tooltip.offsetHeight - margin;
      tooltip.style.left = `${Math.max(margin, Math.min(event.offsetX + 14, maxLeft))}px`;
      tooltip.style.top = `${Math.max(margin, Math.min(event.offsetY + 14, maxTop))}px`;
      draw();
    }

    function renderLegend(data) {
      const counts = data?.counts || {};
      const channels = [...new Set(points.filter(p => p.classification === 'cell').map(p => p.lif_channel).filter(Boolean))].sort();
      const items = [
        ['未标注', COLORS.unknown, counts.unknown || 0],
        ...channels.map(name => [name, stableColor(name), points.filter(p => p.classification === 'cell' && p.lif_channel === name).length]),
      ];
      if (Number(counts.qc || 0) > 0) items.splice(1, 0, ['QC', COLORS.qc, counts.qc]);
      if (Number(counts.conflict || 0)) items.push(['冲突', '#d92d20', counts.conflict]);
      legend.innerHTML = items.map(([label, color, count]) =>
        `<span class="legend-item"><i class="swatch" style="background:${color}"></i>${escapeText(label)} ${count}</span>`
      ).join('');
    }

    async function fetchJson(url) {
      const response = await fetch(url, { cache: 'no-store' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      return data;
    }

    async function loadFullState({ forceFit = false } = {}) {
      try {
        const data = await fetchJson('/api/cell-event-map');
        const nextKey = projectIdentity(data);
        const identityChanged = projectKey !== nextKey;
        if (identityChanged) {
          points = [];
          payload = null;
          revision = '';
          fitted = false;
          hoverPoint = null;
          matchedTimeEventIds = new Set();
          timeStatus.textContent = '';
          tooltip.style.display = 'none';
          draw();
        }
        projectKey = nextKey;
        payload = data;
        if (data.coordinates_available === false) {
          points = [];
          revision = String(data.revision || '');
          identity.textContent = 'UMAP 尚未配置';
          legend.innerHTML = '';
          setEmpty('事件列表可用于 Track 标注，但尚未配置二维 UMAP 坐标。可在主窗口的配置中附加坐标 CSV。');
          draw();
          return;
        }
        points = Array.isArray(data.points) ? data.points : [];
        revision = String(data.revision || '');
        identity.textContent = `${points.length.toLocaleString()} 个事件点`;
        setEmpty(points.length ? '' : '当前项目没有事件坐标点。');
        renderLegend(data);
        if (identityChanged || forceFit || !fitted) fitView();
        else draw();
      } catch (error) {
        projectKey = '';
        payload = null;
        points = [];
        revision = '';
        fitted = false;
        identity.textContent = '没有可用的事件 UMAP';
        legend.innerHTML = '';
        setEmpty(error.message || String(error));
        draw();
      }
    }

    async function pollRevision() {
      try {
        const next = await fetchJson('/api/cell-event-map-revision');
        const nextKey = projectIdentity(next);
        if (nextKey !== projectKey || String(next.revision || '') !== revision) {
          await loadFullState();
        }
      } catch (error) {
        if (projectKey) await loadFullState();
      }
    }

    function highlightTrackEvent(message) {
      if (String(message.project_id || '') !== String(payload?.project_id || '')) return;
      if (String(message.map_sha256 || '') !== String(payload?.map_sha256 || '')) return;
      const eventId = String(message.ms_event_id || '');
      const point = points.find(row => String(row.ms_event_id || '') === eventId);
      if (!point) return;
      matchedTimeEventIds = new Set([eventId]);
      const eventTime = pointMs760Time(point);
      if (Number.isFinite(eventTime)) timeQuery.value = String(Number(eventTime.toFixed(6)));
      timeStatus.textContent = 'Track · 1 point';
      draw();
    }

    function announceUmapReady() {
      if (!channel || !payload) return;
      channel.postMessage({
        type: 'umap-ready',
        project_id: payload.project_id || '',
        map_sha256: payload.map_sha256 || '',
      });
    }

    function findTimePoints() {
      const targetText = String(timeQuery.value).trim();
      const toleranceText = String(timeTolerance.value).trim();
      const target = targetText ? Number(targetText) : NaN;
      const tolerance = toleranceText ? Number(toleranceText) : NaN;
      if (!Number.isFinite(target)) {
        timeStatus.textContent = 'Enter time';
        timeQuery.focus();
        return;
      }
      if (!Number.isFinite(tolerance) || tolerance < 0) {
        timeStatus.textContent = 'Check ± min';
        timeTolerance.focus();
        return;
      }
      const matches = points.filter(point => (
        Number.isFinite(pointMs760Time(point))
        && Math.abs(pointMs760Time(point) - target) <= tolerance
      ));
      matchedTimeEventIds = new Set(matches.map(point => String(point.ms_event_id)));
      if (!matches.length) {
        timeStatus.textContent = 'No match';
        draw();
        return;
      }
      timeStatus.textContent = `${matches.length} point${matches.length === 1 ? '' : 's'}`;
      draw();
    }

    function clearTimeSearch() {
      matchedTimeEventIds = new Set();
      timeQuery.value = '';
      timeStatus.textContent = '';
      draw();
    }

    canvas.addEventListener('pointerdown', event => {
      dragging = true;
      moved = false;
      dragStart = { x: event.clientX, y: event.clientY, tx: view.tx, ty: view.ty };
      canvas.classList.add('dragging');
      canvas.setPointerCapture(event.pointerId);
    });
    canvas.addEventListener('pointermove', event => {
      if (dragging && dragStart) {
        const dx = event.clientX - dragStart.x;
        const dy = event.clientY - dragStart.y;
        moved = moved || Math.abs(dx) + Math.abs(dy) > 3;
        view.tx = dragStart.tx + dx;
        view.ty = dragStart.ty + dy;
        tooltip.style.display = 'none';
        hoverPoint = null;
        draw();
        return;
      }
      showTooltip(event, nearestPoint(event.offsetX, event.offsetY));
    });
    canvas.addEventListener('pointerup', event => {
      if (!moved) {
        const point = nearestPoint(event.offsetX, event.offsetY);
        if (point && channel) {
          channel.postMessage({
            type: 'focus-event',
            project_id: payload?.project_id || '',
            map_sha256: payload?.map_sha256 || '',
            ms_event_id: point.ms_event_id,
            scan_start_time: point.scan_start_time,
          });
        }
      }
      dragging = false;
      dragStart = null;
      canvas.classList.remove('dragging');
      try { canvas.releasePointerCapture(event.pointerId); } catch (_) {}
    });
    canvas.addEventListener('pointerleave', () => {
      if (!dragging) showTooltip({ offsetX: 0, offsetY: 0 }, null);
    });
    canvas.addEventListener('wheel', event => {
      event.preventDefault();
      const oldScale = view.scale;
      const factor = Math.exp(-event.deltaY * .0012);
      const nextScale = Math.max(oldScale * .08, Math.min(oldScale * 80, oldScale * factor));
      const worldX = (event.offsetX - view.tx) / oldScale;
      const worldY = (view.ty - event.offsetY) / oldScale;
      view.scale = nextScale;
      view.tx = event.offsetX - worldX * nextScale;
      view.ty = event.offsetY + worldY * nextScale;
      draw();
    }, { passive: false });
    document.getElementById('fit').addEventListener('click', fitView);
    document.getElementById('timeSearch').addEventListener('submit', event => {
      event.preventDefault();
      findTimePoints();
    });
    document.getElementById('clearTime').addEventListener('click', clearTimeSearch);
    window.addEventListener('resize', resizeCanvas);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) pollRevision();
    });
    if (channel) {
      channel.addEventListener('message', event => {
        const message = event.data || {};
        if (message.type === 'highlight-event') {
          highlightTrackEvent(message);
          return;
        }
        if (['annotation-changed', 'project-changed', 'map-attached', 'map-replaced'].includes(message.type)) {
          pollRevision();
        }
      });
    }

    new ResizeObserver(resizeCanvas).observe(plot);
    loadFullState({ forceFit: true }).then(announceUmapReady);
    setInterval(pollRevision, 2000);
  </script>
</body>
</html>
"""

UMAP_HTML = UMAP_HTML.replace("__LMA_SIGNAL_COLORS__", signal_palette_json())
