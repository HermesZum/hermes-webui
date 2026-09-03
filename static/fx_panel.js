// ═══════════════════════════════════════════════════════════════════════
// FX panel — fx-tracker (HermesZum/fx-tracker)
// Extracted from panels.js (2026-09-03 audit P2). Loaded after panels.js
// (defer order in index.html); relies on globals from ui.js ($, esc),
// i18n.js (I18N), workspace.js (api) and panels.js
// (_closeMobileSidebarAfterPanelSelection).
// ═══════════════════════════════════════════════════════════════════════

// ═══════════════════════════════════════════════════════════════════════
// FX panel — fx-tracker (HermesZum/fx-tracker)
// ═══════════════════════════════════════════════════════════════════════
let _fxData = null;        // { health, reports, notes }
let _fxFilter = '';        // note-type filter chip

async function loadFx(force) {
  const menu = $('fxPanelMenu');
  if (!menu) return;
  if (force) _fxData = null;
  if (!_fxData) {
    try {
      const [health, reports, notes, gate, actions, position, calendar] = await Promise.all([
        api('/api/fx/health', {cache:'no-store', timeoutMs:15000}),
        api('/api/fx/reports', {cache:'no-store', timeoutMs:15000}),
        api('/api/fx/notes', {cache:'no-store', timeoutMs:15000}),
        api('/api/fx/gate', {cache:'no-store', timeoutMs:15000}),
        api('/api/fx/actions', {cache:'no-store', timeoutMs:15000}),
        api('/api/fx/position', {cache:'no-store', timeoutMs:15000}),
        api('/api/fx/calendar', {cache:'no-store', timeoutMs:15000})
      ]);
      _fxData = {health, reports, notes, gate, actions, position, calendar};
    } catch (e) {
      menu.innerHTML = `<div style="padding:12px;color:var(--accent);font-size:12px">${esc('FX: ' + (e && e.message ? e.message : e))}</div>`;
      return;
    }
  }
  _renderFxPanel();
}

function _fxT(key, fallback) {
  try { return I18N.t(key) || fallback; } catch (_) { return fallback; }
}

function _fxGateBadge(val) {
  if (val === true) return `<span class="fx-badge fx-pass">${esc(_fxT('fx_pass','PASS'))}</span>`;
  if (val === false) return `<span class="fx-badge fx-fail">${esc(_fxT('fx_fail','FAIL'))}</span>`;
  return `<span class="fx-badge fx-na">n/a</span>`;
}

function _fxRenderHealth(h) {
  const g = (h && h.guard) || {};
  let guardHtml;
  if (g.error) {
    guardHtml = `<div class="fx-guard fx-guard-error"><span class="fx-badge fx-fail">${esc(_fxT('fx_guard_error','GUARD ERROR'))}</span><span class="fx-muted">${esc(g.error)}</span></div>`;
  } else if (g.ok === true) {
    guardHtml = `<div class="fx-guard fx-guard-ok"><span class="fx-badge fx-pass">${esc(_fxT('fx_guard_ok','CONSISTENCY GUARD: ALL CLEAR'))}</span></div>`;
  } else {
    const lines = (g.contradictions || []).map(l => `<div class="fx-contradiction-line">${esc(l)}</div>`).join('');
    guardHtml = `<div class="fx-guard fx-guard-error"><span class="fx-badge fx-fail">${esc(_fxT('fx_guard_fail','CONTRADICTIONS DETECTED'))}</span>${lines}</div>`;
  }
  const paper = (h && h.paper) || {};
  const ct = (h && h.ctrader) || {};
  const risk = (h && h.risk) || {};
  const paperTxt = paper.error ? esc(paper.error)
    : `${esc(_fxT('fx_open','Open'))}: ${paper.open || 0} · ${esc(_fxT('fx_closed','Closed'))}: ${paper.n_closed || 0} · ΣR: ${paper.realized_r != null ? paper.realized_r : 'n/a'} · ${paper.halted ? esc(_fxT('fx_halted','HALTED')) : esc(_fxT('fx_running','running'))}`;
  const ctTxt = ct.error ? esc(ct.error) : `${esc(ct.status || 'unknown')}${ct.since ? ' · ' + esc(ct.since) : ''}`;
  const riskTxt = risk.error ? esc(risk.error) : `risk ${risk.risk_pct != null ? risk.risk_pct + '%' : 'n/a'} · ${esc(_fxT('fx_pause_at','pause at'))} ${risk.pause_threshold_R != null ? risk.pause_threshold_R + 'R' : 'n/a'}`;
  return `<div class="fx-section"><div class="fx-section-title">${esc(_fxT('fx_health','Health'))}</div>${guardHtml}
    <div class="fx-health-grid">
      <div class="fx-health-cell"><div class="fx-health-label">${esc(_fxT('fx_paper_trader','Paper trader'))}</div><div>${paperTxt}</div></div>
      <div class="fx-health-cell"><div class="fx-health-label">cTrader</div><div>${ctTxt}</div></div>
      <div class="fx-health-cell"><div class="fx-health-label">${esc(_fxT('fx_risk_config','Risk config'))}</div><div>${riskTxt}</div></div>
    </div></div>`;
}

function _fxRenderReports(r) {
  const reports = (r && r.reports) || [];
  const cards = reports.map(c => {
    if (!c.available) {
      return `<div class="fx-report-card fx-report-missing"><div class="fx-report-title">${esc(c.label)}</div><div class="fx-muted">${esc(c.reason || 'unavailable')}</div></div>`;
    }
    const d = c.data || {};
    const gateBits = [];
    if (d.gate_pass_persymbol !== undefined) {
      gateBits.push(_fxGateBadge(d.gate_pass_persymbol) + '<span class="fx-gate-label">per-symbol (authoritative)</span>');
      if (d.gate_pass !== undefined && d.gate_pass !== d.gate_pass_persymbol) {
        gateBits.push(_fxGateBadge(d.gate_pass) + '<span class="fx-gate-label">legacy applied-config</span>');
      }
    } else {
      if (d.gate_pass !== undefined) gateBits.push(_fxGateBadge(d.gate_pass) + '<span class="fx-gate-label">canonical</span>');
      if (d.gate_pass_applied !== undefined) gateBits.push(_fxGateBadge(d.gate_pass_applied) + '<span class="fx-gate-label">applied</span>');
    }
    if (d.survival_pass !== undefined) gateBits.push(_fxGateBadge(d.survival_pass) + '<span class="fx-gate-label">survival</span>');
    const gate = gateBits.length ? `<div class="fx-report-gates">${gateBits.join('')}</div>` : '';
    const briefing = d.briefing_line ? `<div class="fx-briefing">${esc(d.briefing_line)}</div>` : '';
    const when = c.mtime ? new Date(c.mtime * 1000).toISOString().slice(0, 16).replace('T', ' ') : '';
    return `<div class="fx-report-card"><div class="fx-report-head"><span class="fx-report-title">${esc(c.label)}</span><span class="fx-report-when">${when} UTC</span></div>${gate}${briefing}</div>`;
  }).join('');
  return `<div class="fx-section"><div class="fx-section-title">${esc(_fxT('fx_reports','Reports'))}</div>${cards || `<div class="fx-muted">${esc(_fxT('fx_no_reports','No reports found'))}</div>`}</div>`;
}

function _fxRenderNotes(n) {
  const all = (n && n.notes) || [];
  const chips = [''].concat(['decision','reference','strategy','plan','incident']);
  const chipHtml = chips.map(t => `<button class="fx-chip${_fxFilter === t ? ' fx-chip-on' : ''}" onclick="_fxSetFilter('${t}')">${esc(t === '' ? _fxT('fx_all','all') : t)}</button>`).join('');
  const notes = _fxFilter ? all.filter(x => x.type === _fxFilter) : all;
  const rows = notes.map(x => `
    <div class="fx-note-row" title="${esc(x.path)}">
      <span class="fx-badge fx-type-${esc(x.type)}">${esc(x.type)}</span>
      <span class="fx-note-title">${esc(x.title)}</span>
      <span class="fx-note-date">${esc(x.updated || x.created || '')}</span>
    </div>`).join('');
  const count = `${notes.length}${all.length !== notes.length ? ' / ' + all.length : ''}`;
  return `<div class="fx-section"><div class="fx-section-title">${esc(_fxT('fx_notes','Notes'))} <span class="fx-note-count">${count}</span></div>
    <div class="fx-chips">${chipHtml}</div>
    <div class="fx-notes-list">${rows || `<div class="fx-muted">${esc(_fxT('fx_no_notes','No notes found'))}</div>`}</div></div>`;
}

function _fxSetFilter(t) { _fxFilter = t === _fxFilter ? '' : t; _renderFxPanel(); }

// ── Sections (Memory pattern: side-menu items + main detail view) ──
// Each section = one entry + one render fn; add new sections here.
const FX_SECTIONS = [
  { key: 'gate',     labelKey: 'fx_gate',     fallback: 'Graduation Gate', icon: 'award',   dataKey: 'gate',     render: () => _fxRenderGate((_fxData.gate || {})) },
  { key: 'actions',  labelKey: 'fx_actions',  fallback: 'Action Required', icon: 'bell',    dataKey: 'actions',  render: () => _fxRenderActions((_fxData.actions || {})) },
  { key: 'position', labelKey: 'fx_position', fallback: 'Live Position',   icon: 'crosshair', dataKey: 'position', render: () => _fxRenderPosition((_fxData.position || {})) },
  { key: 'calendar', labelKey: 'fx_calendar', fallback: 'Calendar',        icon: 'clock',   dataKey: 'calendar', render: () => _fxRenderCalendar((_fxData.calendar || {})) },
  { key: 'health',   labelKey: 'fx_health',   fallback: 'Health',          icon: 'activity', dataKey: 'health',  render: () => _fxRenderHealth((_fxData.health || {})) },
  { key: 'reports',  labelKey: 'fx_reports',  fallback: 'Reports',         icon: 'file-text', dataKey: 'reports', render: () => _fxRenderReports((_fxData.reports || {})) },
  { key: 'notes',    labelKey: 'fx_notes',    fallback: 'Notes',           icon: 'book-open', dataKey: 'notes',   render: () => _fxRenderNotes((_fxData.notes || {})) },
];
let _currentFxSection = null;

function _fxIcon(key) {
  // Minimal inline icon set (stroke style matches the app's SVG icons)
  const icons = {
    activity: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    award: '<circle cx="12" cy="8" r="7"/><polyline points="8.21 13.89 7 23 12 20 17 23 15.79 13.88"/>',
    bell: '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
    crosshair: '<circle cx="12" cy="12" r="10"/><line x1="22" y1="12" x2="18" y2="12"/><line x1="6" y1="12" x2="2" y2="12"/><line x1="12" y1="6" x2="12" y2="2"/><line x1="12" y1="22" x2="12" y2="18"/>',
    clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    'file-text': '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    'book-open': '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
    chart: '<line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/>',
  };
  const p = icons[key] || icons.chart;
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
}

function openFxSection(key, el) {
  _currentFxSection = key;
  document.querySelectorAll('#fxPanelMenu .side-menu-item').forEach(e => e.classList.remove('active'));
  if (el) el.classList.add('active');
  _renderFxDetail();
  if (typeof _closeMobileSidebarAfterPanelSelection === 'function') _closeMobileSidebarAfterPanelSelection();
}

function _renderFxMenu() {
  const panel = $('fxPanelMenu');
  if (!panel) return;
  panel.innerHTML = '';
  for (const s of FX_SECTIONS) {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'side-menu-item';
    if (_currentFxSection === s.key) el.classList.add('active');
    el.innerHTML = `${_fxIcon(s.icon)}<span>${esc(_fxT(s.labelKey, s.fallback))}</span>`;
    el.onclick = () => openFxSection(s.key, el);
    panel.appendChild(el);
  }
}

function _renderFxDetail() {
  const title = $('fxDetailTitle');
  const body = $('fxDetailBody');
  const empty = $('fxDetailEmpty');
  const refresh = $('fxDetailRefresh');
  if (!title || !body || !empty) return;
  const s = FX_SECTIONS.find(x => x.key === _currentFxSection);
  if (!s || !_fxData) {
    title.textContent = '';
    body.style.display = 'none';
    if (refresh) refresh.style.display = 'none';
    empty.style.display = '';
    return;
  }
  empty.style.display = 'none';
  if (refresh) refresh.style.display = '';
  title.textContent = _fxT(s.labelKey, s.fallback);
  const err = _fxData[s.dataKey] && _fxData[s.dataKey].error
    ? `<div class="fx-error">${esc(_fxData[s.dataKey].error)}</div>` : '';
  body.innerHTML = err + s.render();
  body.style.display = '';
}

// ── Section renderers: graduation gate ──────────────────────────────────
function _fxPassBadge(pass) {
  if (pass === true) return '<span class="fx-badge fx-badge-pass">PASS</span>';
  if (pass === false) return '<span class="fx-badge fx-badge-fail">PENDING</span>';
  return '<span class="fx-badge">?</span>';
}

function _fxRenderGate(g) {
  if (!g.available) return `<div class="fx-error">${esc(g.error || 'gate unavailable')}</div>`;
  const crit = (g.criteria || []).map(c => `
    <div class="fx-row">
      <span>${esc(c.label)}${c.note ? ` <small style="color:var(--muted)">(${esc(c.note)})</small>` : ''}</span>
      <span>${_fxPassBadge(c.pass)} <b>${c.value === null || c.value === undefined ? '—' : esc(String(c.value))}</b></span>
    </div>`).join('');
  const verdictCls = g.ready ? 'fx-badge-pass' : '';
  const src = g.source ? `<div class="fx-muted" style="font-size:11px;margin-bottom:10px">ledger: ${esc(g.source)}${g.n_open !== undefined ? ` · ${g.n_open} open` : ''}</div>` : '';
  return `<div class="main-view-content">
    <div class="fx-verdict ${verdictCls}">${esc(g.verdict || '')}</div>
    ${src}
    <div class="fx-section">
      <div class="fx-section-title">Criteria</div>
      ${crit}
    </div>
    <div class="fx-section">
      <div class="fx-section-title">Ledger</div>
      <div class="fx-row"><span>Trades</span><b>${g.n_trades}</b></div>
      <div class="fx-row"><span>Total R</span><b>${g.total_r}</b></div>
      <div class="fx-row"><span>Expectancy</span><b>${g.expectancy_R} R</b></div>
      <div class="fx-row"><span>Win rate</span><b>${g.win_rate_pct}%</b></div>
    </div>
  </div>`;
}

// ── Section renderers: actions ──────────────────────────────────────────
function _fxSevBadge(sev) {
  const cls = { HALT: 'fx-badge-fail', ACTION: 'fx-badge-warn' }[sev] || '';
  return `<span class="fx-badge ${cls}">${esc(sev || 'WATCH')}</span>`;
}

function _fxRenderActions(a) {
  if (!a.available) return `<div class="fx-error">${esc(a.error || 'actions unavailable')}</div>`;
  const evs = a.events || [];
  if (!evs.length) return `<div class="fx-verdict fx-badge-pass">CLEAR — nothing needs attention</div>`;
  const rows = evs.map(e => `
    <div class="fx-row">
      <span><small style="color:var(--muted)">${esc(e.source || '')}</small><br>${esc(e.text || '')}</span>
      ${_fxSevBadge(e.severity)}
    </div>`).join('');
  return `<div class="main-view-content"><div class="fx-section">${rows}</div></div>`;
}

// ── Section renderers: live position ────────────────────────────────────
function _fxRenderPosition(p) {
  if (!p.available) return `<div class="fx-error">${esc(p.error || 'position unavailable')}</div>`;
  const pos = p.positions || [];
  if (!pos.length) return `<div class="fx-verdict">No open positions — trader flat${p.halted ? ' (HALTED)' : ''}</div>`;
  const cards = pos.map(o => `
    <div class="fx-section">
      <div class="fx-section-title">${esc(o.pair)} · ${esc(o.dir || '').toUpperCase()}${o.half_closed ? ' · half banked' : ''}</div>
      <div class="fx-row"><span>Entry</span><b>${esc(String(o.entry))}</b></div>
      <div class="fx-row"><span>Stop</span><b>${esc(String(o.stop))}</b></div>
      <div class="fx-row"><span>Entry time</span><b>${esc(String(o.entry_time || ''))}</b></div>
      <div class="fx-row"><span>ATR (pips)</span><b>${esc(String(o.atr_pips))}</b></div>
      <div class="fx-row"><span>Banked R</span><b>${o.banked_r}</b></div>
    </div>`).join('');
  return `<div class="main-view-content">${p.halted ? '<div class="fx-error">TRADER HALTED</div>' : ''}${cards}
    <div class="fx-section"><div class="fx-section-title">Session</div>
      <div class="fx-row"><span>Closed trades</span><b>${p.n_closed}</b></div>
      <div class="fx-row"><span>Realized R</span><b>${p.realized_r_total}</b></div>
    </div></div>`;
}

// ── Section renderers: calendar ─────────────────────────────────────────
function _fxRenderCalendar(c) {
  if (!c.available) return `<div class="fx-error">${esc(c.error || 'calendar unavailable')}</div>`;
  const evs = c.events || [];
  if (!evs.length) return `<div class="fx-verdict">No upcoming high-impact events this week</div>`;
  const rows = evs.map(e => {
    const cur = (e.currencies || []).join('/');
    const lock = e.hours_away <= 0.083; // 5 min before event
    return `<div class="fx-row${lock ? ' fx-row-alert' : ''}">
      <span><b>${esc(String(e.hours_away))}h</b> · ${esc(e.title || '')} <small style="color:var(--muted)">(${esc(cur)} · ${esc(e.impact || '')})</small></span>
      <span><small>${esc(String(e.utc || ''))}</small></span>
    </div>`;
  }).join('');
  return `<div class="main-view-content">
    <div class="fx-section-title">Order-time gate: no entries 5 min before / 15 min after high-impact events</div>
    <div class="fx-section">${rows}</div></div>`;
}

function _renderFxPanel() {
  _renderFxMenu();
  _renderFxDetail();
}
