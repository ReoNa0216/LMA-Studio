"""Standalone high-DPI Canvas page for the project cell-event UMAP."""

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
    .legend { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px 12px; font-size: 12px; }
    .legend-item { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; }
    .swatch { width: 9px; height: 9px; border-radius: 50%; border: 1px solid rgba(0,0,0,.12); }
    button {
      width: 34px;
      height: 32px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--surface);
      color: var(--ink);
      font-size: 16px;
      cursor: pointer;
    }
    button:hover { border-color: #98a2b3; background: #f9fafb; }
    .plot { min-height: 0; position: relative; }
    canvas { display: block; width: 100%; height: 100%; cursor: grab; touch-action: none; }
    canvas.dragging { cursor: grabbing; }
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
      <span class="spacer"></span>
      <div id="legend" class="legend"></div>
      <button id="fit" type="button" title="适配全部点" aria-label="适配全部点">⌂</button>
    </header>
    <section id="plot" class="plot">
      <canvas id="canvas" aria-label="单细胞事件 UMAP"></canvas>
      <div id="empty" class="empty"></div>
      <div id="tooltip" class="tooltip"></div>
    </section>
  </main>
  <script>
    const COLORS = {
      unknown: '#98A2B3',
      qc: '#111827',
      conflict: '#ffffff',
      G1: '#2f6fed',
      G2: '#176b45',
      R1: '#6f4bb8',
      R2: '#b95d18',
    };
    const FALLBACK = ['#0e7490','#be123c','#a16207','#7c3aed','#047857','#c2410c','#4338ca','#9f1239'];
    const channel = ('BroadcastChannel' in window) ? new BroadcastChannel('lma-studio-state-v1') : null;
    const canvas = document.getElementById('canvas');
    const ctx = canvas.getContext('2d');
    const plot = document.getElementById('plot');
    const tooltip = document.getElementById('tooltip');
    const empty = document.getElementById('empty');
    const identity = document.getElementById('identity');
    const legend = document.getElementById('legend');
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

    function escapeText(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[char]));
    }

    function stableColor(channelName) {
      const name = String(channelName || '').toUpperCase();
      if (COLORS[name]) return COLORS[name];
      let hash = 2166136261;
      for (const char of name) {
        hash ^= char.charCodeAt(0);
        hash = Math.imul(hash, 16777619);
      }
      return FALLBACK[Math.abs(hash >>> 0) % FALLBACK.length];
    }

    function pointColor(point) {
      if (point.classification === 'qc') return COLORS.qc;
      if (point.classification === 'cell') return stableColor(point.lif_channel);
      return COLORS.unknown;
    }

    function setEmpty(message) {
      empty.textContent = message || '';
      empty.style.display = message ? 'grid' : 'none';
    }

    function projectIdentity(data) {
      return `${data?.project_id || ''}:${data?.map_sha256 || ''}`;
    }

    function bounds() {
      if (!points.length) return null;
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      for (const point of points) {
        minX = Math.min(minX, Number(point.UMAP1));
        maxX = Math.max(maxX, Number(point.UMAP1));
        minY = Math.min(minY, Number(point.UMAP2));
        maxY = Math.max(maxY, Number(point.UMAP2));
      }
      return { minX, maxX, minY, maxY };
    }

    function fitView() {
      const box = bounds();
      if (!box || !canvas.clientWidth || !canvas.clientHeight) return;
      const padding = 34;
      const dx = Math.max(box.maxX - box.minX, 1e-6);
      const dy = Math.max(box.maxY - box.minY, 1e-6);
      const usableW = Math.max(canvas.clientWidth - padding * 2, 1);
      const usableH = Math.max(canvas.clientHeight - padding * 2, 1);
      view.scale = Math.min(usableW / dx, usableH / dy);
      view.tx = canvas.clientWidth / 2 - ((box.minX + box.maxX) / 2) * view.scale;
      view.ty = canvas.clientHeight / 2 + ((box.minY + box.maxY) / 2) * view.scale;
      fitted = true;
      draw();
    }

    function screenPoint(point) {
      return {
        x: Number(point.UMAP1) * view.scale + view.tx,
        y: -Number(point.UMAP2) * view.scale + view.ty,
      };
    }

    function resizeCanvas() {
      const ratio = Math.max(1, window.devicePixelRatio || 1);
      const width = Math.max(1, Math.round(canvas.clientWidth * ratio));
      const height = Math.max(1, Math.round(canvas.clientHeight * ratio));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      if (!fitted && points.length) fitView();
      else draw();
    }

    function draw() {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      ctx.clearRect(0, 0, width, height);
      if (!points.length) return;
      const radius = Math.max(2.2, Math.min(4.2, 2.6 + Math.log10(Math.max(view.scale, 1)) * .35));
      for (const point of points) {
        const screen = screenPoint(point);
        if (screen.x < -10 || screen.y < -10 || screen.x > width + 10 || screen.y > height + 10) continue;
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
      }
    }

    function statusText(point) {
      if (point.classification === 'qc') return 'QC';
      if (point.classification === 'cell') {
        return [point.lif_channel, point.label].filter(Boolean).join(' · ') || '细胞';
      }
      if (point.classification === 'conflict') {
        const relations = (point.accepted_relations || []).map(row =>
          `${row.kind === 'qc' ? 'QC' : (row.lif_channel || '细胞')}: ${row.annotation_id}`
        );
        return `冲突：${relations.join('；')}`;
      }
      return '未标注';
    }

    function nearestPoint(x, y) {
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
        <strong>MS760 ${Number(point.scan_start_time).toFixed(6)} min</strong>
        <div class="muted">event: ${escapeText(point.ms_event_id)}</div>
        <div class="muted">scan: ${escapeText(point.scan_id)}</div>
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
        ['QC', COLORS.qc, counts.qc || 0],
        ...channels.map(name => [name, stableColor(name), points.filter(p => p.classification === 'cell' && p.lif_channel === name).length]),
      ];
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
          tooltip.style.display = 'none';
          draw();
        }
        projectKey = nextKey;
        payload = data;
        points = Array.isArray(data.points) ? data.points : [];
        revision = String(data.revision || '');
        identity.textContent = `${points.length} points · ${String(data.project_id || '').slice(0, 12)}`;
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
    window.addEventListener('resize', resizeCanvas);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) pollRevision();
    });
    if (channel) {
      channel.addEventListener('message', event => {
        const message = event.data || {};
        if (['annotation-changed', 'project-changed', 'map-attached'].includes(message.type)) {
          pollRevision();
        }
      });
    }

    new ResizeObserver(resizeCanvas).observe(plot);
    loadFullState({ forceFit: true });
    setInterval(pollRevision, 2000);
  </script>
</body>
</html>
"""
