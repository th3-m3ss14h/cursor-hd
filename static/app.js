'use strict';

const state = {
  themes: [],
  selectedPaths: new Set(),
  activeThemePath: null,
  filter: '',
  jobTimer: null,
  activeJobId: null,
  busy: false,
  starting: false,
  lastLogLength: 0,
  activeScanId: null,
  scanRunning: false,
  upscaleBackend: 'lanczos',
  availableModels: null,
};

const $ = (selector) => document.querySelector(selector);

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try {
    data = await response.json();
  } catch (_) {
    throw new Error(`HTTP ${response.status}`);
  }
  if (!response.ok) {
    const error = new Error(data.error || `HTTP ${response.status}`);
    error.data = data;
    throw error;
  }
  return data;
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

function realCursors(theme) {
  return (theme.cursors || []).filter((cursor) => !cursor.is_symlink);
}

function gnomeState(theme) {
  const scalable = theme.gnome51 || { status: 'missing' };
  if (scalable.status === 'compatible') {
    return {
      label: scalable.origin === 'vector' ? 'Vector ready' : 'Scalable ready',
      className: 'status-good',
      detail: `${scalable.valid || 0} valid shapes and ${scalable.aliases || 0} aliases.`,
    };
  }
  if (scalable.status === 'partial') {
    return {
      label: 'Partial',
      className: 'status-warn',
      detail: `${scalable.valid || 0} valid shapes; ${(scalable.missing || []).length} legacy cursor names missing.`,
    };
  }
  if (scalable.status === 'invalid') {
    return { label: 'Invalid scalable data', className: 'status-bad', detail: 'Existing scalable metadata could not be validated.' };
  }
  return { label: 'Needs upgrade', className: 'status-muted', detail: 'No usable GNOME 51 scalable layer was found.' };
}

function visibleThemes() {
  const query = state.filter.trim().toLocaleLowerCase();
  if (!query) return state.themes;
  return state.themes.filter((theme) =>
    `${theme.name} ${theme.id} ${theme.path} ${theme.location}`.toLocaleLowerCase().includes(query)
  );
}

function updateScanProgress(scan) {
  const panel = $('#scan-progress-panel');
  let message = scan.message || 'Scanning installed cursor themes…';
  if (scan.theme && !['theme_start', 'theme_done', 'themes_found', 'complete'].includes(scan.phase)) {
    message = `${scan.theme} · ${message}`;
  }
  $('#scan-progress-message').textContent = message;
  let percent = 8;
  let count = 'Working';
  if (Number.isFinite(scan.themes_total) && scan.themes_total > 0) {
    const done = Math.max(0, scan.themes_done || 0);
    const themeTotal = scan.themes_total;
    const cursorTotal = scan.cursor_total || 0;
    const cursorIndex = scan.cursor_index || 0;
    const fraction = cursorTotal > 0 ? Math.min(0.95, Math.max(0, cursorIndex / cursorTotal)) : 0.05;
    percent = Math.min(99, Math.max(2, ((done + (scan.phase === 'theme_done' ? 0 : fraction)) / themeTotal) * 100));
    count = `${Math.min(done, themeTotal)} / ${themeTotal} themes`;
    if (scan.theme_index && scan.phase !== 'complete') count += ` · ${scan.theme_index} current`;
    if (cursorTotal > 0) count += ` · ${cursorIndex}/${cursorTotal} items`;
  } else if (scan.root_index && scan.roots_total) {
    percent = Math.min(35, Math.max(5, (scan.root_index / scan.roots_total) * 35));
    count = `Searching ${scan.root_index}/${scan.roots_total} locations`;
  }
  const bar = $('#scan-progress-bar');
  bar.style.width = `${percent}%`;
  $('#scan-progress-track').setAttribute('aria-valuenow', String(Math.round(percent)));
  panel.classList.toggle('is-indeterminate', scan.status === 'running' && (!scan.themes_total || scan.phase === 'discovery'));
  panel.classList.toggle('is-complete', scan.status === 'complete');
  panel.classList.toggle('is-error', scan.status === 'error');
  if (scan.status === 'complete') {
    bar.style.width = '100%';
    count = `${scan.themes_total || 0} themes · ${scan.elapsed_seconds ?? 'done'}s`;
  }
  if (scan.status === 'error') count = 'Scan failed';
  $('#scan-progress-track').setAttribute('aria-valuenow', String(scan.status === 'complete' ? 100 : Math.round(percent)));
  $('#scan-progress-count').textContent = count;
  $('#scan-meta').textContent = scan.status === 'complete'
    ? count
    : scan.status === 'error' ? 'Scan failed' : 'Scanning…';
}

function showScannedThemes(themes) {
  state.themes = themes;
  const currentPaths = new Set(state.themes.map((theme) => theme.path));
  for (const path of [...state.selectedPaths]) {
    if (!currentPaths.has(path)) state.selectedPaths.delete(path);
  }
  if (state.activeThemePath && !currentPaths.has(state.activeThemePath)) state.activeThemePath = null;
  renderList();
  renderSelection();
  if (state.activeThemePath) {
    const active = state.themes.find((theme) => theme.path === state.activeThemePath);
    if (active) renderDetails(active);
    else renderEmptyDetails();
  } else if (state.themes.length) {
    renderDetails(state.themes[0]);
  } else {
    renderEmptyDetails('No installed cursor themes were found.');
  }
  const ready = state.themes.filter((theme) => theme.gnome51?.status === 'compatible').length;
  const low = state.themes.filter((theme) => theme.low_res).length;
  $('#scan-meta').textContent = `${state.themes.length} themes · ${ready} already GNOME 51 ready · ${low} low-resolution`;
}

async function loadThemes() {
  if (state.scanRunning) return;
  state.scanRunning = true;
  $('#scan-meta').textContent = 'Starting scan…';
  $('#rescan-btn').disabled = true;
  $('#scan-progress-panel').classList.remove('is-complete', 'is-error');
  $('#scan-progress-panel').classList.add('is-indeterminate');
  $('#scan-progress-message').textContent = 'Starting background scan of installed cursor themes…';
  $('#scan-progress-count').textContent = 'Starting';
  $('#scan-progress-bar').style.width = '10%';
  try {
    const started = await fetchJson('/api/scan');
    state.activeScanId = started.scan_id;
    while (state.activeScanId) {
      await new Promise((resolve) => setTimeout(resolve, 300));
      const scan = await fetchJson(`/api/scan/${encodeURIComponent(state.activeScanId)}`);
      updateScanProgress(scan);
      if (scan.status === 'complete') {
        showScannedThemes(scan.result || []);
        break;
      }
      if (scan.status === 'error') {
        throw new Error(scan.error || 'Unknown scanning error');
      }
    }
  } catch (error) {
    $('#scan-meta').textContent = 'Scan failed';
    $('#scan-progress-message').textContent = `Scan failed: ${error.message}`;
    $('#scan-progress-count').textContent = 'Error';
    $('#scan-progress-panel').classList.remove('is-indeterminate', 'is-complete');
    $('#scan-progress-panel').classList.add('is-error');
    if (!state.themes.length) {
      $('#theme-rows').innerHTML = `<div class="error-state">${esc(error.message)}</div>`;
    }
  } finally {
    state.activeScanId = null;
    state.scanRunning = false;
    $('#rescan-btn').disabled = false;
    $('#scan-progress-panel').classList.remove('is-indeterminate');
  }
}

function renderList() {
  const container = $('#theme-rows');
  const themes = visibleThemes();
  if (!themes.length) {
    container.innerHTML = '<div class="loading-state">No themes match this filter.</div>';
    return;
  }
  container.innerHTML = '';
  const fragment = document.createDocumentFragment();
  for (const theme of themes) {
    const status = gnomeState(theme);
    const row = document.createElement('div');
    row.className = 'theme-row';
    if (theme.path === state.activeThemePath) row.classList.add('active');
    if (state.selectedPaths.has(theme.path)) row.classList.add('checked');
    row.innerHTML = `
      <input class="theme-checkbox" type="checkbox" aria-label="Select ${esc(theme.name)}" ${state.selectedPaths.has(theme.path) ? 'checked' : ''} ${state.busy ? 'disabled' : ''}>
      <button class="theme-row-main" type="button" aria-label="Inspect ${esc(theme.name)}">
        <span class="theme-row-title">${esc(theme.name)}</span>
        <span class="theme-row-location">${esc(theme.location || theme.install_scope || '')}</span>
        <span class="theme-row-status ${status.className}">${esc(status.label)}</span>
      </button>
      <span class="native-size ${theme.low_res ? 'native-low' : ''}">${theme.max_native_size ? `${theme.max_native_size}px` : 'n/a'}</span>
    `;
    row.querySelector('.theme-checkbox').addEventListener('change', (event) => {
      if (event.target.checked) state.selectedPaths.add(theme.path);
      else state.selectedPaths.delete(theme.path);
      renderList();
      renderSelection();
    });
    row.querySelector('.theme-row-main').addEventListener('click', () => {
      state.activeThemePath = theme.path;
      renderList();
      renderDetails(theme);
    });
    fragment.appendChild(row);
  }
  container.appendChild(fragment);
}

function renderSelection() {
  const count = state.selectedPaths.size;
  $('#sel-count').textContent = `${count} selected`;
  $('#upgrade-selected-btn').disabled = count === 0 || state.busy;
  $('#upgrade-selected-btn').textContent = count ? `Upgrade ${count} theme${count === 1 ? '' : 's'} →` : 'Upgrade selected →';
  $('#select-visible-btn').disabled = state.busy || visibleThemes().length === 0;
  $('#clear-selection-btn').disabled = state.busy || count === 0;
}

function renderEmptyDetails(message = 'Select a cursor theme') {
  $('#detail').innerHTML = `
    <div class="empty-state">
      <div class="empty-mark">↗</div>
      <h1>${esc(message)}</h1>
      <p>Select one or more themes from the left. The tool inspects each installed theme and upgrades the full theme in one pass.</p>
    </div>`;
}

function plannedAction(theme) {
  if (['User data', 'Home icons'].includes(theme.install_scope)) {
    return {
      heading: 'Update this installation in place',
      body: 'The existing theme ID and display name are retained. Its Xcursor files are rebuilt with a 4× size ladder, then its GNOME 51 scalable layer is refreshed.',
    };
  }
  return {
    heading: 'Create or update a user-local override',
    body: `The source under ${theme.location || 'the system icon directories'} will remain untouched. An override using the same theme ID (${theme.id}) will be written under the XDG user icon directory so the desktop can use it without a duplicate “HD” theme.`,
  };
}

function renderDetails(theme) {
  state.activeThemePath = theme.path;
  renderList();
  const cursors = theme.cursors || [];
  const real = realCursors(theme);
  const aliases = cursors.filter((cursor) => cursor.is_symlink);
  const animated = real.filter((cursor) => cursor.animated);
  const status = gnomeState(theme);
  const action = plannedAction(theme);
  const missing = (theme.gnome51?.missing || []).slice(0, 12);
  const invalid = (theme.gnome51?.invalid_names || []).slice(0, 8);
  const problems = [
    ...(theme.errors || []).map((error) => `<li>${esc(error)}</li>`),
    ...invalid.map((item) => `<li>${esc(item.name)}: ${esc(item.reason)}</li>`),
  ];
  $('#detail').innerHTML = `
    <section class="detail-header">
      <div class="eyebrow">CURSOR THEME</div>
      <div class="detail-title-row">
        <div>
          <h1>${esc(theme.name)}</h1>
          <div class="detail-id">Theme ID: <code>${esc(theme.id)}</code></div>
        </div>
        <span class="status-badge ${status.className}">${esc(status.label)}</span>
      </div>
      <div class="path-block"><span>Installed at</span><code>${esc(theme.path)}</code></div>
    </section>

    <section class="stats-grid" aria-label="Theme details">
      <div class="stat-card"><span>Cursor shapes</span><strong>${real.length}</strong><small>compiled Xcursor files</small></div>
      <div class="stat-card"><span>Aliases</span><strong>${aliases.length}</strong><small>including internal lookup names</small></div>
      <div class="stat-card"><span>Animated</span><strong>${animated.length}</strong><small>multi-frame cursors</small></div>
      <div class="stat-card"><span>Native maximum</span><strong>${theme.max_native_size || 'n/a'}${theme.max_native_size ? 'px' : ''}</strong><small>${theme.low_res ? 'low-resolution source' : 'largest discovered frame'}</small></div>
    </section>

    <section class="info-section">
      <div class="section-heading"><h2>Upgrade plan</h2><span class="plan-chip">Automatic · full theme</span></div>
      <div class="plan-panel">
        <strong>${esc(action.heading)}</strong>
        <p>${esc(action.body)}</p>
        <div class="plan-facts">
          <span>4× cursor size ladder</span><span>GNOME 51 scalable SVG metadata</span><span>Keep cursor aliases and animation</span><span>Preserve original vector art where present</span>
        </div>
      </div>
    </section>

    <section class="info-section">
      <div class="section-heading"><h2>GNOME 51 compatibility</h2><span class="status-inline ${status.className}">${esc(status.label)}</span></div>
      <p class="section-copy">${esc(status.detail)} ${theme.gnome51?.origin === 'vector' ? 'Existing native vector artwork was detected.' : theme.gnome51?.origin === 'raster' ? 'Existing scalable cursors are raster-backed; the upgrade regenerates them from the rebuilt cursor frames.' : 'The upgrade generates scalable cursor metadata from the rebuilt cursor frames.'}</p>
      ${missing.length ? `<div class="missing-list"><span>Names needing an alias or generated shape</span><p>${missing.map(esc).join(', ')}${(theme.gnome51?.missing || []).length > missing.length ? `, +${(theme.gnome51.missing || []).length - missing.length} more` : ''}</p></div>` : ''}
      ${problems.length ? `<details class="diagnostics"><summary>Diagnostics (${problems.length})</summary><ul>${problems.join('')}</ul></details>` : ''}
    </section>

    <section class="info-section cursor-inventory">
      <div class="section-heading"><h2>Cursor inventory</h2><span class="section-meta">${cursors.length} names</span></div>
      <div class="table-wrap">
        <table class="cursor-table">
          <thead><tr><th>Cursor name</th><th>Native sizes</th><th>Frames</th><th>Type</th></tr></thead>
          <tbody>${cursors.map((cursor) => {
            if (cursor.is_symlink) {
              return `<tr class="alias-row"><td><code>${esc(cursor.name)}</code></td><td colspan="2">↳ ${esc(cursor.target || 'alias')}</td><td>Alias</td></tr>`;
            }
            return `<tr><td><code>${esc(cursor.name)}</code></td><td>${(cursor.nominal_sizes || []).map((size) => `${size}px`).join(', ') || 'n/a'}</td><td>${cursor.frame_count || 0}</td><td>${cursor.animated ? `<span class="mini-tag animated-tag">Animated</span>` : '<span class="mini-tag">Static</span>'}</td></tr>`;
          }).join('')}</tbody>
        </table>
      </div>
    </section>
  `;
}

function openConfirmModal() {
  const chosen = state.themes.filter((theme) => state.selectedPaths.has(theme.path));
  if (!chosen.length || state.busy) return;
  state.upscaleBackend = 'lanczos';
  const chips = chosen.map((theme) => `
    <div class="confirm-theme">
      <div><strong>${esc(theme.name)}</strong><span>${esc(theme.path)}</span></div>
      <span class="confirm-max">${theme.max_native_size ? `${theme.max_native_size}px` : 'n/a'}</span>
    </div>`).join('');
  $('#confirm-body').innerHTML = `
    <div class="confirm-intro">The full cursor set in each selected theme will be processed using the same upscaling method. No per-theme choices or separately named copies are created.</div>
    <div class="confirm-list">${chips}</div>
    <section class="upscale-picker" aria-labelledby="upscale-heading">
      <div class="upscale-picker-heading">
        <strong id="upscale-heading">Upscaling method</strong>
        <span>One choice for the entire batch</span>
      </div>
      <div class="upscale-options">
        <label class="upscale-option is-selected" data-backend-option="lanczos" for="backend-lanczos">
          <input id="backend-lanczos" type="radio" name="upscale-backend" value="lanczos" checked>
          <span class="upscale-option-copy"><strong>Lanczos <em>CPU</em></strong><small>Reliable conventional resampling. No AI model or CUDA runtime needed.</small></span>
        </label>
        <label class="upscale-option is-disabled" data-backend-option="realesrgan" for="backend-realesrgan">
          <input id="backend-realesrgan" type="radio" name="upscale-backend" value="realesrgan" disabled>
          <span class="upscale-option-copy"><strong>Real-ESRGAN <em>GPU</em></strong><small id="model-status">Checking for a supported checkpoint in <code>weights/</code>…</small></span>
        </label>
      </div>
      <p class="upscale-preflight-note">When GPU mode is selected, the model, Python dependencies, and CUDA inference are tested before any theme files are changed. A failed check stops the batch; it never silently falls back to Lanczos.</p>
    </section>
    <div class="confirm-method">
      <div><strong>Output</strong><p>Every selected theme receives a 4× cursor size ladder and a GNOME 51 scalable cursor tree. Existing vector artwork is preserved where available.</p></div>
      <div><strong>Where it goes</strong><p>Writable user themes are updated in place. System-installed themes receive a same-ID user-local override; their system files remain untouched.</p></div>
      <div><strong>Safety check</strong><p>The complete selection is checked for path collisions before work starts. Unrelated themes are never overwritten silently.</p></div>
    </div>`;

  $('#confirm-modal').classList.remove('hidden');
  $('#confirm-start-btn').disabled = false;
  $('#confirm-cancel-btn').disabled = false;
  $('#confirm-start-btn').textContent = 'Upgrade themes';

  for (const input of document.querySelectorAll('input[name="upscale-backend"]')) {
    input.addEventListener('change', () => {
      if (!input.checked || input.disabled) return;
      state.upscaleBackend = input.value;
      document.querySelectorAll('[data-backend-option]').forEach((option) => {
        option.classList.toggle('is-selected', option.dataset.backendOption === state.upscaleBackend);
      });
    });
  }
  loadUpscaleModels();
}

async function loadUpscaleModels() {
  const gpuInput = $('#backend-realesrgan');
  const gpuOption = document.querySelector('[data-backend-option="realesrgan"]');
  const status = $('#model-status');
  if (!gpuInput || !gpuOption || !status) return;
  gpuInput.disabled = true;
  gpuOption.classList.add('is-disabled');
  status.textContent = 'Checking for a supported checkpoint in weights/…';
  try {
    const models = await fetchJson('/api/models');
    state.availableModels = models;
    if (!models.length) {
      status.textContent = 'Unavailable: add a supported .pth checkpoint to the project weights/ directory.';
      if (state.upscaleBackend === 'realesrgan') {
        state.upscaleBackend = 'lanczos';
        $('#backend-lanczos').checked = true;
      }
      return;
    }
    const preferred = models[0];
    gpuInput.disabled = false;
    gpuOption.classList.remove('is-disabled');
    status.textContent = `Found ${preferred.name}. CUDA, dependencies, and a test inference are checked before processing.`;
  } catch (error) {
    status.textContent = `Could not inspect weights/: ${error.message}`;
  } finally {
    document.querySelectorAll('[data-backend-option]').forEach((option) => {
      option.classList.toggle('is-selected', option.dataset.backendOption === state.upscaleBackend);
    });
  }
}

async function startUpgrade() {
  const paths = [...state.selectedPaths];
  if (!paths.length || state.busy) return;
  state.starting = true;
  $('#confirm-start-btn').disabled = true;
  $('#confirm-cancel-btn').disabled = true;
  $('#confirm-start-btn').textContent = state.upscaleBackend === 'realesrgan' ? 'Checking GPU/model…' : 'Starting…';
  try {
    const data = await fetchJson('/api/upgrade_batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme_paths: paths, backend: state.upscaleBackend }),
    });
    state.busy = true;
    state.activeJobId = data.job_id;
    state.lastLogLength = 0;
    $('#joblog').dataset.lastKey = '';
    $('#confirm-modal').classList.add('hidden');
    $('#jobpanel').classList.remove('hidden');
    $('#job-title').textContent = `Upgrading ${paths.length} theme${paths.length === 1 ? '' : 's'}…`;
    $('#job-subtitle').textContent = `${state.upscaleBackend === 'realesrgan' ? 'Real-ESRGAN GPU' : 'Lanczos CPU'} · 4× size ladder + GNOME 51 scalable generation`;
    $('#joblog').innerHTML = '';
    $('#job-progress').style.width = '0%';
    renderList();
    renderSelection();
    await pollJob(data.job_id);
  } catch (error) {
    if (error.data?.fallback_available) {
      const useCpu = window.confirm(
        `Real-ESRGAN could not start. No themes were changed.\n\n${error.message}\n\nUse Lanczos CPU instead?`
      );
      if (useCpu) {
        state.upscaleBackend = 'lanczos';
        $('#backend-lanczos').checked = true;
        state.starting = false;
        $('#confirm-start-btn').disabled = false;
        $('#confirm-cancel-btn').disabled = false;
        $('#confirm-start-btn').textContent = 'Upgrade themes';
        return startUpgrade();
      }
    } else {
      alert(error.message);
    }
  } finally {
    state.starting = false;
    $('#confirm-start-btn').disabled = false;
    $('#confirm-cancel-btn').disabled = false;
    $('#confirm-start-btn').textContent = 'Upgrade themes';
  }
}

async function refreshChangedThemes(results) {
  const paths = [...new Set((results || []).filter((item) => item.status === 'ok' && item.source).map((item) => item.source))];
  if (!paths.length) return;
  await Promise.all(paths.map(async (path) => {
    try {
      const updated = await fetchJson(`/api/theme?path=${encodeURIComponent(path)}`);
      const index = state.themes.findIndex((theme) => theme.path === path);
      if (index >= 0) state.themes[index] = updated;
    } catch (_) {
      // Keep the existing inventory if an individual refresh fails.
    }
  }));
  renderList();
  renderSelection();
  if (state.activeThemePath) {
    const active = state.themes.find((theme) => theme.path === state.activeThemePath);
    if (active) renderDetails(active);
  }
}

function showFallbackPrompt(job) {
  const modal = $('#fallback-modal');
  if (!modal) return;
  $('#fallback-error').textContent = job.fallback_error || 'Real-ESRGAN failed while processing the selected themes.';
  $('#fallback-theme').textContent = `Theme ${job.themes_done + 1} of ${job.themes_total} was rolled back.`;
  modal.dataset.jobId = job.id;
  modal.classList.remove('hidden');
}

function closeFallbackPrompt() {
  $('#fallback-modal')?.classList.add('hidden');
}

async function chooseCpuFallback() {
  const modal = $('#fallback-modal');
  const jobId = modal?.dataset.jobId;
  if (!jobId) return;
  $('#fallback-cpu-btn').disabled = true;
  $('#fallback-cancel-btn').disabled = true;
  try {
    await fetchJson(`/api/job/${encodeURIComponent(jobId)}/fallback`, { method: 'POST' });
    closeFallbackPrompt();
    $('#job-title').textContent = 'Resuming with CPU fallback…';
    $('#job-subtitle').textContent = 'Lanczos CPU · same batch · no rescan';
    await pollJob(jobId);
  } catch (error) {
    alert(error.message);
    $('#fallback-cpu-btn').disabled = false;
    $('#fallback-cancel-btn').disabled = false;
  }
}

async function cancelFallback() {
  const modal = $('#fallback-modal');
  const jobId = modal?.dataset.jobId;
  if (!jobId) return;
  $('#fallback-cpu-btn').disabled = true;
  $('#fallback-cancel-btn').disabled = true;
  try {
    await fetchJson(`/api/job/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
    closeFallbackPrompt();
    state.busy = false;
    state.activeJobId = null;
    renderList();
    renderSelection();
    $('#job-title').textContent = 'Upgrade operation cancelled';
    $('#job-subtitle').textContent = 'No rescan was started';
    await pollJob(jobId);
  } catch (error) {
    alert(error.message);
    $('#fallback-cpu-btn').disabled = false;
    $('#fallback-cancel-btn').disabled = false;
  }
}

function appendLog(entry) {
  const log = $('#joblog');
  const item = document.createElement('div');
  item.className = `log-entry ${entry.status === 'error' ? 'log-error' : entry.status === 'ok' ? 'log-ok' : ''}`;
  const phase = entry.phase ? `${entry.phase} · ` : '';
  const theme = entry.theme ? `${entry.theme} · ` : '';
  const cursor = entry.cursor ? `${entry.cursor} ` : '';
  const detail = entry.error || entry.message || (entry.status === 'ok' ? 'done' : entry.status || 'working');
  item.innerHTML = `<span class="log-prefix">${esc(theme + phase + cursor)}</span><span>${esc(detail)}</span>`;
  log.appendChild(item);
  while (log.children.length > 800) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

async function pollJob(jobId) {
  if (state.jobTimer) clearTimeout(state.jobTimer);
  try {
    const job = await fetchJson(`/api/job/${encodeURIComponent(jobId)}`);
    const fullLog = job.log || [];
    for (const entry of fullLog.slice(state.lastLogLength)) appendLog(entry);
    state.lastLogLength = fullLog.length;
    if (job.themes_total) {
      const percent = Math.round((job.themes_done || 0) / job.themes_total * 100);
      $('#job-progress').style.width = `${percent}%`;
      $('#job-subtitle').textContent = `${job.themes_done || 0} / ${job.themes_total} themes processed`;
    }
    if (job.status === 'running') {
      state.jobTimer = setTimeout(() => pollJob(jobId), 500);
      return;
    }
    if (job.status === 'needs_fallback') {
      state.busy = true;
      showFallbackPrompt(job);
      return;
    }
    state.busy = false;
    state.activeJobId = null;
    renderList();
    renderSelection();
    if (job.status === 'error') {
      $('#job-title').textContent = 'Upgrade operation failed';
      appendLog({ status: 'error', phase: 'operation', error: job.error || 'Unknown error' });
    } else if (job.status === 'cancelled') {
      $('#job-title').textContent = 'Upgrade operation cancelled';
    } else {
      const result = job.result || {};
      const succeeded = result.succeeded ?? (result.themes || []).filter((theme) => theme.status === 'ok').length;
      const failed = result.failed ?? ((result.themes || []).length - succeeded);
      $('#job-title').textContent = failed ? `Finished with ${failed} failure${failed === 1 ? '' : 's'}` : `Upgrade complete · ${succeeded} theme${succeeded === 1 ? '' : 's'}`;
      $('#job-progress').style.width = '100%';
      for (const theme of (result.themes || [])) {
        if (theme.status === 'ok') {
          appendLog({ theme: theme.theme, phase: 'complete', status: 'ok', cursor: theme.out_dir, message: `GNOME 51 output: ${theme.scalable_dir}` });
        } else {
          appendLog({ theme: theme.theme, phase: 'failed', status: 'error', cursor: theme.out_dir, error: theme.error });
        }
      }
    }
    await refreshChangedThemes((job.result || {}).themes || job.partial_results || []);
  } catch (error) {
    state.busy = false;
    renderList();
    renderSelection();
    $('#job-title').textContent = 'Could not read job status';
    appendLog({ status: 'error', error: error.message });
  }
}

$('#theme-filter').addEventListener('input', (event) => {
  state.filter = event.target.value;
  renderList();
});
$('#select-visible-btn').addEventListener('click', () => {
  for (const theme of visibleThemes()) state.selectedPaths.add(theme.path);
  renderList();
  renderSelection();
});
$('#clear-selection-btn').addEventListener('click', () => {
  state.selectedPaths.clear();
  renderList();
  renderSelection();
});
$('#upgrade-selected-btn').addEventListener('click', openConfirmModal);
$('#confirm-cancel-btn').addEventListener('click', () => {
  if (!state.starting) $('#confirm-modal').classList.add('hidden');
});
$('#confirm-start-btn').addEventListener('click', startUpgrade);
$('#fallback-cpu-btn').addEventListener('click', chooseCpuFallback);
$('#fallback-cancel-btn').addEventListener('click', cancelFallback);
$('#rescan-btn').addEventListener('click', loadThemes);
$('#job-close').addEventListener('click', () => $('#jobpanel').classList.add('hidden'));

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && !state.starting) $('#confirm-modal').classList.add('hidden');
});

loadThemes();
