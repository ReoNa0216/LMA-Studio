"""Compact native feature/UMAP controls inside the existing project dialog."""

FEATURE_PANEL = r'''
      <section id="nativeFeaturePanel" class="attach-map-panel" aria-labelledby="nativeFeatureTitle" tabindex="-1">
        <p id="nativeFeatureTitle" class="side-title">矩阵与原生 UMAP</p>
        <p id="nativeFeatureSummary" class="attach-map-copy">正在读取矩阵…</p>
        <div class="attach-map-actions">
          <button id="importNativeFeatures" class="small-button secondary" type="button">从 ZIP 导入矩阵…</button>
        </div>
        <p id="nativeScope" class="attach-map-copy" role="status" aria-live="polite"></p>
        <details style="margin-top:12px;">
          <summary>UMAP 计算设置</summary>
          <div class="policy-fields" style="margin-top:10px;">
            <label for="nativePcs">主成分数<input id="nativePcs" type="number" min="1" max="200" step="1" value="50" /></label>
            <label for="nativeNeighbors">邻居数<input id="nativeNeighbors" type="number" min="2" max="200" step="1" value="15" /></label>
            <label for="nativeSeed">随机种子<input id="nativeSeed" type="number" min="0" max="2147483647" step="1" value="1" /></label>
          </div>
          <p class="attach-map-copy">计算副本补零 → PCA → 邻居图 → UMAP；不改原矩阵，不做归一化或批次校正。小样本自动减少主成分和邻居数。</p>
          <p id="nativeActualSettings" class="attach-map-copy" hidden></p>
        </details>
        <div class="attach-map-actions" style="margin-top:12px;">
          <button id="runNativeUmap" class="small-button" type="button" aria-describedby="nativeScope" disabled>计算 UMAP</button>
          <button id="useNativeUmap" class="small-button secondary" type="button" hidden>使用已保存 UMAP</button>
          <span id="nativeViewField" hidden><label for="nativeUmapView">坐标来源</label>
          <select id="nativeUmapView" aria-label="坐标来源" disabled>
            <option value="base">导入的 CSV 坐标</option>
            <option value="native">原生 UMAP</option>
          </select></span>
        </div>
        <p id="nativeFeatureStatus" class="attach-map-copy" role="status" aria-live="polite"></p>
      </section>
'''

FEATURE_SCRIPT = r'''
    let nativePollTimer = null;
    let nativeBusy = false;
    let nativeRequest = 0;
    let nativeMetaPending = false;
    let nativeInfo = null;
    let nativeInfoProject = null;

    function nativeScopeReady(info) {
      const saved = state.meta?.project_config || {};
      const draft = state.configProtocolDraft;
      const segments = draft?.segments || [];
      const confirmed = draft?.compatibility_mode
        ? info.selection_scope.boundaries_confirmed
        : segments.length > 0 && segments.every(row => row.boundaries_confirmed);
      const comparable = rows => JSON.stringify((rows || []).map(row =>
        [Number(row.start_min), Number(row.end_min), Boolean(row.boundaries_confirmed)]));
      const unsaved = !info.selection_scope.boundaries_confirmed
        || comparable(segments) !== comparable(saved.calibration_protocol?.segments)
        || !el('cfgAnnotationStart').value.trim()
        || Number(el('cfgAnnotationStart').value) !== Number(saved.annotation_start_min);
      return {confirmed, unsaved, ready: confirmed && !unsaved};
    }

    function refreshNativePrerequisites() {
      if (nativeInfo && nativeInfoProject === state.meta?.project_id) renderFeaturePanel(nativeInfo);
    }

    async function updateFeatureMeta(project, request) {
      const response = await fetch('/api/meta');
      const meta = await response.json();
      if (!response.ok) throw new Error(meta.error || '项目读取失败');
      if (project !== state.meta?.project_id || meta.project_id !== project || request !== nativeRequest) return false;
      state.meta = meta;
      syncUmapButtonState();
      updateAttachMapControls();
      notifyStateChannel('native-umap');
      return true;
    }

    function renderFeaturePanel(info) {
      nativeInfo = info;
      nativeInfoProject = state.meta?.project_id;
      nativeBusy = info.job?.status === 'running';
      const prerequisite = nativeScopeReady(info);
      el('nativeFeatureSummary').textContent = info.available
        ? `${info.events.toLocaleString()} 个事件 × ${info.features.toLocaleString()} 个 feature。`
        : (info.can_import ? '从 LMA 事件包 ZIP 补充同一批事件的矩阵。'
          : '此项目使用已有坐标；接入原生矩阵需用 LMA 事件包另建项目。');
      el('importNativeFeatures').disabled = nativeBusy || !info.can_import;
      el('runNativeUmap').disabled = nativeBusy || state.configSaveBusy || !info.available || !prerequisite.ready;
      el('nativeViewField').hidden = !(info.base_coordinates_available && info.has_umap);
      el('useNativeUmap').hidden = !info.has_umap || info.base_coordinates_available || info.view === 'native';
      el('useNativeUmap').disabled = nativeBusy;
      el('runNativeUmap').textContent = nativeBusy ? '计算中…'
        : !info.available ? '计算 UMAP'
        : !prerequisite.confirmed ? '先确认前段边界'
        : prerequisite.unsaved ? '先保存项目配置'
        : info.has_umap ? '重新计算 UMAP' : '计算 UMAP';
      el('nativeUmapView').disabled = nativeBusy || !info.has_umap;
      el('nativeUmapView').value = info.view;
      for (const id of ['nativePcs','nativeNeighbors','nativeSeed']) el(id).disabled = nativeBusy;
      el('nativeActualSettings').hidden = !info.has_umap || !info.actual_parameters;
      const actual = info.actual_parameters;
      el('nativeActualSettings').textContent = info.has_umap && actual
        ? `已保存结果：${actual.n_pcs} 个主成分，${actual.n_neighbors} 个邻居，随机种子 ${actual.random_state}。` : '';
      const scope = info.selection_scope;
      el('nativeScope').textContent = !info.available ? ''
        : !prerequisite.confirmed
        ? '先勾选上方各参考段的“边界已确认”，再点击底部“保存项目配置”。'
        : prerequisite.unsaved
        ? '前段设置尚未保存。请点击底部“保存项目配置”，保存后即可计算。'
        : `计算 ${scope.start_ns / 60000000000} min 起的事件；此前事件不参与，后段 QC 暂保留。`;
      if (!prerequisite.ready && !nativeBusy) {
        el('nativeFeatureStatus').textContent = '';
      } else if (info.job?.status === 'failed') {
        el('nativeFeatureStatus').textContent = `计算失败：${info.job.message}。已有坐标保持不变。`;
      } else if (info.scope_warning && !nativeBusy) {
        el('nativeFeatureStatus').textContent = info.scope_warning;
      } else if (info.has_umap && info.actual_parameters && !nativeBusy) {
        el('nativeFeatureStatus').textContent = `UMAP 已保存：${Number(info.umap_events).toLocaleString()} 个事件点。可从顶部 UMAP 查看。`;
      } else el('nativeFeatureStatus').textContent = info.job?.message || '';
    }

    async function refreshFeaturePanel() {
      const project = state.meta?.project_id;
      const request = ++nativeRequest;
      if (!project) return;
      try {
        const response = await fetch('/api/feature-umap');
        const info = await response.json();
        if (!response.ok) throw new Error(info.error || '读取失败');
        if (project !== state.meta?.project_id || request !== nativeRequest) return;
        const wasBusy = nativeBusy;
        renderFeaturePanel(info);
        if (nativePollTimer) clearTimeout(nativePollTimer);
        if (nativeBusy) {
          nativePollTimer = setTimeout(refreshFeaturePanel, 1500);
        } else if (wasBusy || nativeMetaPending) {
          nativeMetaPending = true;
          if (await updateFeatureMeta(project, request)) {
            nativeMetaPending = false;
            state.actionBusy = false;
          }
        }
      } catch (error) {
        el('nativeFeatureStatus').textContent = `读取失败：${error.message}`;
        if (nativeBusy || nativeMetaPending) nativePollTimer = setTimeout(refreshFeaturePanel, 2500);
      }
    }

    el('importNativeFeatures').addEventListener('click', async () => {
      if (state.actionBusy || nativeBusy) return;
      const project = state.meta.project_id;
      state.actionBusy = true;
      el('importNativeFeatures').disabled = true;
      try {
        const picked = await postJson('/api/select-path', {kind:'file', file_role:'ms_results', title:'选择 LMA 事件包 ZIP'});
        if (picked.cancelled || !picked.path || project !== state.meta.project_id) return;
        el('nativeFeatureStatus').textContent = '正在核对事件与矩阵…';
        await postJson('/api/feature-umap/import', {project_id:project, source_path:picked.path});
        el('nativeFeatureStatus').textContent = '矩阵已导入。点击“计算 UMAP”生成坐标。';
        nativeMetaPending = true;
        if (await updateFeatureMeta(project, nativeRequest)) nativeMetaPending = false;
      } catch (error) {
        el('nativeFeatureStatus').textContent = `导入失败：${error.message}`;
      } finally {
        if (!nativeMetaPending) state.actionBusy = false;
        await refreshFeaturePanel();
      }
    });

    el('runNativeUmap').addEventListener('click', async () => {
      if (state.actionBusy || nativeBusy) return;
      if (!nativeInfo || !nativeScopeReady(nativeInfo).ready) {
        refreshNativePrerequisites();
        return;
      }
      const inputs = ['nativePcs','nativeNeighbors','nativeSeed'].map(el);
      if (inputs.some(input => !input.reportValidity() || !input.value)) return;
      state.actionBusy = true;
      el('runNativeUmap').disabled = true;
      try {
        await postJson('/api/feature-umap/run', {project_id:state.meta.project_id,
          parameters:{n_pcs:Number(inputs[0].value), n_neighbors:Number(inputs[1].value), random_state:Number(inputs[2].value)}});
        nativeBusy = true;
        await refreshFeaturePanel();
      } catch (error) {
        state.actionBusy = false;
        refreshNativePrerequisites();
        el('nativeFeatureStatus').textContent = `计算失败：${error.message}`;
      }
    });

    async function selectCoordinateView(view) {
      if (state.actionBusy || nativeBusy) return;
      state.actionBusy = true;
      const project = state.meta.project_id;
      try {
        await postJson('/api/feature-umap/view', {project_id:state.meta.project_id, view});
        nativeMetaPending = true;
        if (await updateFeatureMeta(project, nativeRequest)) nativeMetaPending = false;
        el('nativeFeatureStatus').textContent = '坐标视图已切换，标注与时间模型保持不变。';
      } catch (error) {
        el('nativeFeatureStatus').textContent = `切换失败：${error.message}`;
      } finally {
        if (!nativeMetaPending) state.actionBusy = false;
        await refreshFeaturePanel();
      }
    }
    el('nativeUmapView').addEventListener('change', () => selectCoordinateView(el('nativeUmapView').value));
    el('useNativeUmap').addEventListener('click', () => selectCoordinateView('native'));
'''
