(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const fmt = n => Number(n || 0).toLocaleString('en-US');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const bytes = b => { const u = ['B', 'KB', 'MB', 'GB']; let i = 0; b = Number(b || 0); while (b >= 1024 && i < 3) { b /= 1024; i++; } return `${b.toFixed(i && b < 100 ? 1 : 0)} ${u[i]}`; };
  const ms = v => v == null ? '–' : `${v < 10 ? Number(v).toFixed(1) : Math.round(v)} ms`;
  const secs = s => s < 60 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, '0')}s`;
  const pct = (a, b) => b ? `${Math.round((a / b) * 100)}%` : '–';
  const compact = n => n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${(n / 1e3).toFixed(1)}k` : fmt(Math.round(n));
  const ts = at => new Date(at).getTime();

  async function api(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) {
      let msg = `${r.status}`;
      try { const j = await r.json(); msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail); } catch (_) { /* keep status */ }
      throw new Error(msg);
    }
    return r.json();
  }

  let toastT;
  function toast(msg, err) {
    const t = $('#toast');
    t.textContent = msg; t.classList.toggle('err', !!err); t.classList.add('on');
    clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('on'), 3200);
  }

  // number tween
  function tween(el, to, f = fmt) {
    const from = el._v ?? 0;
    el._v = to;
    if (from === to) { el.textContent = f(to); return; }
    const t0 = performance.now(), d = 450;
    cancelAnimationFrame(el._raf);
    const step = now => {
      const k = Math.min(1, (now - t0) / d), e = 1 - Math.pow(1 - k, 3);
      el.textContent = f(Math.round(from + (to - from) * e));
      if (k < 1) el._raf = requestAnimationFrame(step);
    };
    el._raf = requestAnimationFrame(step);
  }

  const S = {
    cfg: null, id: null, imp: null, view: null, timer: null, lastEvent: 0, phase: 'idle',
    preview: null, m: { phones: [], name: null, vars: [], country: null, region: 'IN', landline: false, infer: true },
    run: null, res: null, lockedAt: null,
  };

  function freshRun() {
    return {
      chunks: [], sums: { rows: 0, ok: 0, dup: 0, bad: 0, fast: 0 }, reasons: {},
      t0: null, queuedAt: null, claimedAt: null, doneAt: null, lastAt: null, chunkSize: S.cfg?.chunk_size || 10000,
      queue: [], playing: false, worker: null, resumeFrom: 0, finalized: false, total: 0, error: null,
    };
  }

  // ------------------------------------------------------------ views + rail
  function showView(name) {
    if (S.view === name) return;
    S.view = name;
    $$('.view').forEach(v => { if (v.id !== `v-${name}`) v.classList.remove('on', 'shown'); });
    const el = $(`#v-${name}`);
    el.classList.add('shown');
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('on')));
    $('#newBtn').classList.toggle('hidden', name === 'idle');
  }

  function stage(name, state, metric, bar) {
    const el = $(`.st[data-s="${name}"]`);
    el.classList.toggle('active', state === 'active');
    el.classList.toggle('done', state === 'done');
    el.classList.toggle('fail', state === 'fail');
    if (metric !== undefined) el.querySelector('.mt').innerHTML = metric;
    el.querySelector('.bar').style.width = state === 'active' && bar != null ? `${bar}%` : '';
  }

  function mapSummary(m) {
    if (!m) return '';
    const p = m.phone_columns || m.phones || [];
    const v = m.var_columns || m.vars || [];
    return `${p.length} phone · ${v.length} var${v.length === 1 ? '' : 's'}`;
  }

  function renderRail() {
    const imp = S.imp, R = S.run, ph = S.phase;
    const order = ['upload', 'map', 'queue', 'claim', 'process', 'fetch'];
    if (ph === 'idle') { order.forEach((s, i) => stage(s, i === 0 ? 'active' : '', '')); return; }
    if (ph === 'uploading') return;
    const upMetric = imp ? `${bytes(imp.file_size)} · ~${compact(imp.est_rows)} rows` : '';
    stage('upload', 'done', upMetric);
    if (ph === 'uploaded') {
      stage('map', 'active', mapSummary(S.m));
      ['queue', 'claim', 'process', 'fetch'].forEach(s => stage(s, '', ''));
      return;
    }
    stage('map', 'done', mapSummary(imp.mapping));
    const q = S.queueInfo;
    if (ph === 'queued') {
      stage('queue', 'active', q ? `ahead ${q.ahead} · running ${q.running}/${q.slots}` : 'waiting');
      ['claim', 'process', 'fetch'].forEach(s => stage(s, '', ''));
      return;
    }
    const waited = R.claimedAt && R.queuedAt ? secs(Math.max(0, R.claimedAt - R.queuedAt) / 1000) : '';
    stage('queue', 'done', waited ? `waited ${waited}` : '');
    const n = R.chunks.length, total = Math.max(R.total, n);
    if (ph === 'processing') {
      stage('claim', 'done', `<i class="beat" id="beat"></i>${esc(R.worker || '')}`);
      stage('process', 'active', `${n} / ${total} chunks`, total ? (n / total) * 100 : 0);
      stage('fetch', '', '');
    } else if (ph === 'done') {
      stage('claim', 'done', esc(R.worker || ''));
      stage('process', 'done', `${n} chunks · ${secs(((R.doneAt || R.lastAt) - R.claimedAt) / 1000)}`);
      stage('fetch', 'done', `${fmt(R.sums.ok)} contacts`);
    } else if (ph === 'failed') {
      const where = R.claimedAt ? 'process' : 'claim';
      stage('claim', R.claimedAt ? 'done' : 'fail', esc(R.worker || ''));
      stage('process', where === 'process' ? 'fail' : '', 'failed');
      stage('fetch', '', '');
    }
  }

  function heartbeat() {
    const b = $('#beat');
    if (!b) return;
    b.classList.remove('on'); void b.offsetWidth; b.classList.add('on');
  }

  function crumb() {
    const c = $('#crumb');
    if (!S.imp) { c.classList.remove('on'); return; }
    c.innerHTML = `#${S.imp.id} <b>·</b> ${esc(S.imp.file_name || '')} <b>· campaign ${S.imp.campaign_id}</b>`;
    c.classList.add('on');
  }

  // ------------------------------------------------------------ idle + upload
  async function loadRecent() {
    try {
      const items = await api('/api/imports?limit=30');
      $('#recentCount').textContent = items.length ? items.length : '';
      $('#recent').innerHTML = items.length ? items.map((it, i) => `
        <div class="ri" data-id="${it.id}" style="animation:rise .35s ${i * 25}ms both cubic-bezier(.2,.7,.2,1)">
          <i class="sd ${esc(it.status)}"></i>
          <div><div class="n">${esc(it.file_name || 'upload.csv')}</div><div class="s">#${it.id} · campaign ${it.campaign_id} · ${esc(it.status)}</div></div>
          <div class="s">${it.status === 'done' ? fmt(it.valid_rows) : bytes(it.file_size)}</div>
        </div>`).join('') : '<div class="empty">nothing yet</div>';
    } catch (e) { $('#recent').innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }

  function showIdle() {
    stopPoll();
    S.id = null; S.imp = null; S.phase = 'idle'; S.run = freshRun();
    crumb(); renderRail();
    const d = $('#drop'); d.classList.remove('busy', 'over');
    $('#upBar').style.width = '0'; $('#upTrack').classList.remove('indet');
    $('#dropHint').textContent = `or click to choose · up to ${bytes(S.cfg?.max_upload_bytes || 0)}`;
    showView('idle');
    loadRecent();
  }

  function scrambleSha(el, sha) {
    const hex = '0123456789abcdef', t0 = performance.now(), d = 700;
    const step = now => {
      const k = Math.min(1, (now - t0) / d), fixed = Math.floor(sha.length * k);
      el.innerHTML = sha.split('').map((c, i) => i < fixed ? `<span>${c}</span>` : `<span class="new">${hex[(Math.random() * 16) | 0]}</span>`).join('');
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function upload(file) {
    if (S.phase === 'uploading') return;
    const max = S.cfg?.max_upload_bytes || Infinity;
    if (file.size > max) { toast(`${file.name} is ${bytes(file.size)}; limit is ${bytes(max)}`, true); return; }
    const cid = parseInt($('#campaign').value, 10) || 1;
    S.phase = 'uploading';
    const d = $('#drop'); d.classList.add('busy');
    $('#upName').textContent = file.name;
    $('#upSha').textContent = 'computing while streaming';
    $('#upRows').textContent = '–'; $('#upRate').textContent = '–'; $('#upBytes').textContent = `0 B / ${bytes(file.size)}`;
    stage('upload', 'active', '0%', 0);
    const t0 = performance.now();
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/imports?campaign_id=${cid}`);
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name));
    xhr.setRequestHeader('Content-Type', 'text/csv');
    xhr.upload.onprogress = e => {
      const p = e.lengthComputable ? (e.loaded / e.total) * 100 : 0;
      const dt = (performance.now() - t0) / 1000;
      $('#upBar').style.width = `${p}%`;
      $('#upPct').textContent = `${Math.floor(p)}%`;
      $('#upBytes').textContent = `${bytes(e.loaded)} / ${bytes(e.total)}`;
      if (dt > .1) $('#upRate').textContent = `${bytes(e.loaded / dt)}/s`;
      stage('upload', 'active', `${Math.floor(p)}% · ${bytes(e.loaded)}`, p);
    };
    xhr.upload.onload = () => { $('#upTrack').classList.add('indet'); $('#upPct').textContent = 'finalizing'; };
    xhr.onerror = () => { toast('upload failed', true); S.phase = 'idle'; showIdle(); };
    xhr.onload = () => {
      $('#upTrack').classList.remove('indet'); $('#upBar').style.width = '100%';
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* non-json error */ }
      if (xhr.status !== 200 && xhr.status !== 202) {
        toast(body.detail || `upload failed (${xhr.status})`, true); S.phase = 'idle'; showIdle(); return;
      }
      if (body.duplicate) {
        $('#upPct').textContent = 'already imported';
        toast(`same file already uploaded to campaign ${cid} as #${body.import_id}`);
        setTimeout(() => { location.hash = body.import_id; }, 900);
        return;
      }
      $('#upPct').textContent = '100%';
      scrambleSha($('#upSha'), body.sha256);
      $('#upRows').textContent = `~${fmt(body.est_rows)} (newline count)`;
      stage('upload', 'done', `${bytes(body.bytes)} · ~${compact(body.est_rows)} rows`);
      setTimeout(() => { location.hash = body.import_id; }, 1300);
    };
    xhr.send(file);
  }

  // ------------------------------------------------------------ mapping
  async function rescore() {
    // Validity in the sample depends on the country column and default country, so re-score on change.
    const q = new URLSearchParams({ country_column: S.m.country || '', region: $('#region').value || S.m.region });
    try {
      const p = await api(`/api/imports/${S.id}/preview?${q}`);
      if (S.view !== 'map' || !S.preview) return;
      S.preview.columns = p.columns;
      renderCols();
      renderMapping();
    } catch (_) { /* keep the current scores */ }
  }

  async function loadPreview() {
    const p = await api(`/api/imports/${S.id}/preview`);
    S.preview = p;
    const src = p.mapping || p.suggested;
    S.m = {
      phones: [...(src.phone_columns || [])], name: src.name_column || null, vars: [...(src.var_columns || [])],
      country: src.country_column || null, region: p.default_region || 'IN',
      landline: !!src.reject_landline, infer: src.infer_country_code !== false,
    };
    fillRegions();
    $('#region').value = S.m.region;
    $('#landline').checked = S.m.landline;
    $('#inferCc').checked = S.m.infer;
    $('#colFilter').value = '';
    $('#colCount').textContent = `${p.headers.length} · ${p.rows.length} sampled`;
    renderCols();
    renderMapping();
  }

  function fillRegions() {
    const sel = $('#region');
    if (sel.options.length) return;
    const regions = S.cfg?.regions || [['IN', 'India'], ['US', 'United States'], ['GB', 'United Kingdom']];
    sel.innerHTML = regions.map(([code, name]) => `<option value="${esc(code)}">${esc(name)} · ${esc(code)}</option>`).join('');
  }

  function renderCols() {
    const f = $('#colFilter').value.trim().toLowerCase();
    const cols = [...S.preview.columns]
      .filter(c => !f || c.header.toLowerCase().includes(f))
      .sort((a, b) => b.score - a.score || a.index - b.index);
    $('#clist').innerHTML = cols.map((c, i) => {
      const phoney = c.valid_pct > 0 || c.name_score > 0;
      const ex = (c.examples || []).map(([raw, e164]) =>
        `<span title="${esc(raw)}${e164 ? ' → ' + esc(e164) : ''}">${phoney ? `<i class="${e164 ? 'y' : 'n'}"></i>` : ''}${esc(raw)}</span>`).join('');
      return `<div class="cr" data-h="${esc(c.header)}" style="animation-delay:${Math.min(i, 30) * 14}ms">
        <div><div class="h">${esc(c.header) || '<span class="muted">(blank)</span>'}</div><div class="ex">${ex || '<span>empty in sample</span>'}</div></div>
        <div class="meter" title="header match ${c.name_score} · valid ${Math.round(c.valid_pct * 100)}% · filled ${Math.round(c.fill_pct * 100)}%">
          <div class="l"><span>valid</span><span>${Math.round(c.valid_pct * 100)}%</span></div>
          <div class="track"><i style="width:0" data-w="${c.valid_pct * 100}"></i></div>
        </div>
        <div class="tg">
          <button class="chip ph-c" data-act="phone">phone</button>
          <button class="chip nm" data-act="name">name</button>
          <button class="chip ct" data-act="country">country</button>
          <button class="chip vr" data-act="var">var</button>
        </div></div>`;
    }).join('') || '<div class="empty">no match</div>';
    requestAnimationFrame(() => $$('#clist .track i').forEach(i => { i.style.width = `${i.dataset.w}%`; }));
    syncChips();
  }

  function syncChips() {
    $$('#clist .cr').forEach(r => {
      const h = r.dataset.h, pi = S.m.phones.indexOf(h);
      const pc = $('[data-act="phone"]', r);
      pc.classList.toggle('on', pi >= 0);
      pc.textContent = pi >= 0 ? `phone ${pi + 1}` : 'phone';
      $('[data-act="name"]', r).classList.toggle('on', S.m.name === h);
      $('[data-act="var"]', r).classList.toggle('on', S.m.vars.includes(h));
      $('[data-act="country"]', r).classList.toggle('on', S.m.country === h);
      r.classList.toggle('is-phone', pi >= 0);
    });
  }

  function colPct(h) {
    const c = S.preview.columns.find(x => x.header === h);
    return c ? `${Math.round(c.valid_pct * 100)}%` : '';
  }

  function renderMapping() {
    const m = S.m;
    $('#pri').innerHTML = m.phones.length ? m.phones.map((h, i) => `
      <div class="pi" data-h="${esc(h)}">
        <span class="o">${i + 1}</span><span class="t">${esc(h)}</span><span class="p">${colPct(h)}</span>
        <span style="display:flex">
          <button class="ib" data-act="up" title="raise priority" ${i === 0 ? 'disabled style="opacity:.25"' : ''}><svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor"><path d="M2 6.5 5 3.5 8 6.5"/></svg></button>
          <button class="ib" data-act="rm" title="remove"><svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor"><path d="M2.5 2.5l5 5M7.5 2.5l-5 5"/></svg></button>
        </span>
      </div>`).join('') : '<div class="none">pick at least one phone column</div>';
    $('#nameTag').innerHTML = m.name ? `<span class="tag">${esc(m.name)}<button class="ib" data-act="rmname"><svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor"><path d="M2.5 2.5l5 5M7.5 2.5l-5 5"/></svg></button></span>` : '<span class="none">none</span>';
    $('#countryTag').innerHTML = m.country ? `<span class="tag">${esc(m.country)}<button class="ib" data-act="rmcountry"><svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor"><path d="M2.5 2.5l5 5M7.5 2.5l-5 5"/></svg></button></span>` : '<span class="none">none, every row uses the default country</span>';
    $('#varTags').innerHTML = m.vars.length ? m.vars.map(v => `<span class="tag" data-h="${esc(v)}">${esc(v)}<button class="ib" data-act="rmvar"><svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor"><path d="M2.5 2.5l5 5M7.5 2.5l-5 5"/></svg></button></span>`).join('') : '<span class="none">none</span>';
    $('#startBtn').disabled = !m.phones.length;
    syncChips();
    if (S.phase === 'uploaded') stage('map', 'active', mapSummary(m));
  }

  function toggle(act, h) {
    const m = S.m;
    if (act === 'phone') m.phones = m.phones.includes(h) ? m.phones.filter(x => x !== h) : [...m.phones, h];
    else if (act === 'name') m.name = m.name === h ? null : h;
    else if (act === 'country') { m.country = m.country === h ? null : h; renderMapping(); rescore(); return; }
    else if (act === 'var') m.vars = m.vars.includes(h) ? m.vars.filter(x => x !== h) : [...m.vars, h];
    renderMapping();
  }

  async function startImport() {
    const b = $('#startBtn');
    b.disabled = true;
    try {
      await api(`/api/imports/${S.id}/mapping`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          phone_columns: S.m.phones, name_column: S.m.name, var_columns: S.m.vars,
          country_column: S.m.country, default_region: $('#region').value,
          reject_landline: $('#landline').checked, infer_country_code: $('#inferCc').checked,
        }),
      });
      await openImport(S.id);
    } catch (e) { toast(e.message, true); b.disabled = false; }
  }

  // ------------------------------------------------------------ run view
  function resetRun() {
    S.run = freshRun();
    $('#cgrid').innerHTML = ''; $('#log').innerHTML = ''; $('#errSlot').innerHTML = '';
    $('#spRate').innerHTML = ''; $('#spRss').innerHTML = ''; $('#worker').textContent = '';
    $('#results').classList.add('hidden');
    $$('#lane .ls').forEach(l => { $('.v', l).textContent = '–'; $('.s', l).innerHTML = '&nbsp;'; $('.t', l).style.width = '0'; });
    $('#laneChunk').textContent = '';
    ['#cRows', '#cOk', '#cDup', '#cBad'].forEach(s => { const el = $(s); el._v = 0; el.textContent = '0'; });
    $('#cOf').textContent = ''; $('#cPct').textContent = '';
    ['#sbA', '#sbB', '#sbC'].forEach(s => { $(s).style.width = '0'; });
    $('#mEl').textContent = '–'; $('#mRate').textContent = '–'; $('#mFast').textContent = '–';
    renderReasons();
  }

  function cellSize(n) {
    if (n <= 12) return 56;
    if (n <= 30) return 40;
    if (n <= 80) return 26;
    if (n <= 200) return 18;
    return 13;
  }

  function ensureCells(n, instant) {
    const g = $('#cgrid');
    const have = g.children.length;
    for (let i = have; i < n; i++) {
      const c = document.createElement('div');
      c.className = 'cell';
      c.dataset.i = i;
      c.innerHTML = '<i class="a"></i><i class="b"></i><i class="c"></i><b></b>';
      c.style.animationDelay = `${Math.min(i - have, 120) * (instant ? 4 : 8)}ms`;
      g.appendChild(c);
    }
    const size = cellSize(g.children.length);
    g.style.setProperty('--cell', `${size}px`);
    g.classList.toggle('lg', size >= 40);
  }

  function markLive() {
    const R = S.run;
    $$('#cgrid .cell.live').forEach(c => c.classList.remove('live'));
    if (S.phase !== 'processing') return;
    const next = $(`#cgrid .cell[data-i="${R.chunks.length}"]`);
    if (next) next.classList.add('live');
    else if (R.chunks.length >= R.total) { ensureCells(R.chunks.length + 1); $(`#cgrid .cell[data-i="${R.chunks.length}"]`)?.classList.add('live'); }
  }

  function fillCell(c, animate, idx) {
    ensureCells(c.n);
    const el = $(`#cgrid .cell[data-i="${c.n - 1}"]`);
    if (!el) return;
    const rows = Math.max(1, c.rows);
    const [a, b, d] = $$('i', el);
    const delay = animate ? 0 : Math.min(idx, 150) * 6;
    [a, b, d].forEach(x => { x.style.transitionDelay = `${delay}ms`; });
    requestAnimationFrame(() => {
      a.style.height = `${(c.inserted / rows) * 100}%`;
      b.style.height = `${(c.dups / rows) * 100}%`;
      d.style.height = `${(c.rejected / rows) * 100}%`;
    });
    $('b', el).textContent = c.n;
    el.classList.remove('live'); el.classList.add('done');
    if (animate) { el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 260); }
  }

  const LANE = ['read', 'validate', 'copy', 'errors', 'merge', 'checkpoint'];
  function runLane(c, animate) {
    const vals = {
      read: [`${fmt(c.rows)} rows`, ms(c.read_ms), c.read_ms],
      validate: [ms(c.validate_ms), `${c.workers} proc · fast ${pct(c.fast, c.staged)}`, c.validate_ms],
      copy: [`${fmt(c.staged)} rows`, ms(c.copy_ms), c.copy_ms],
      errors: [`${fmt(c.rejected)} rows`, ms(c.errors_ms), c.errors_ms],
      merge: [`+${fmt(c.inserted)}`, `${fmt(c.dups)} dup · ${ms(c.merge_ms)}`, c.merge_ms],
      checkpoint: [`row ${fmt(c.last_row)}`, 'exactly-once', 0],
    };
    const total = LANE.reduce((s, k) => s + (vals[k][2] || 0), 0) || 1;
    const R = S.run;
    $('#laneChunk').textContent = `chunk ${c.n} · rows ${fmt(c.first_row)}–${fmt(c.last_row)}`;
    LANE.forEach((k, i) => {
      const el = $(`#lane .ls[data-k="${k}"]`);
      const apply = () => {
        $('.v', el).textContent = vals[k][0];
        $('.s', el).textContent = vals[k][1];
        $('.t', el).style.width = k === 'checkpoint' ? '100%' : `${Math.max(2, (vals[k][2] / total) * 100)}%`;
      };
      if (!animate) { apply(); return; }
      setTimeout(() => {
        if (S.run !== R) return;  // user navigated to another import meanwhile
        apply(); el.classList.add('hot');
        setTimeout(() => el.classList.remove('hot'), 280);
      }, i * 55);
    });
  }

  function spark(el, vals, color, label, floorMax) {
    if (!vals.length) { el.innerHTML = ''; return; }
    const w = 300, h = 60;
    const max = Math.max(floorMax || 0, ...vals) * 1.35 || 1;
    const pts = vals.length === 1 ? [[0, vals[0]], [w, vals[0]]] : vals.map((v, i) => [(i / (vals.length - 1)) * w, v]);
    const d = pts.map(([x, v], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${(h - (v / max) * (h - 6) - 1).toFixed(1)}`).join('');
    el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path class="ar" d="${d}L${w},${h}L0,${h}Z" fill="${color}"/><path class="ln" d="${d}" stroke="${color}"/></svg><div class="last">${label}</div>`;
  }

  function renderSparks() {
    const R = S.run;
    const rates = [], rss = [];
    // Rate over the chunk's own work (read + validate + COPY + merge), so queue waits and restarts don't skew it.
    R.chunks.forEach(c => {
      const work = (c.read_ms + c.validate_ms + c.copy_ms + c.errors_ms + c.merge_ms) / 1000;
      rates.push(c.rows / Math.max(0.001, work));
      rss.push(c.rss_mb);
    });
    spark($('#spRate'), rates, 'var(--accent)', rates.length ? `${compact(rates[rates.length - 1])} rows/s` : '');
    const peak = Math.max(...rss, 0);
    spark($('#spRss'), rss, 'var(--ok)', rss.length ? `${rss[rss.length - 1]} MB · peak ${peak} MB` : '', peak * 1.6);
  }

  const REASONS = {
    empty: 'no value in any mapped phone column',
    invalid: 'not a dialable number',
    sci_notation: 'Excel 9.19E+11, digits already lost',
    landline: 'fixed line, rejected by option',
  };
  function renderReasons() {
    const r = S.run.reasons, total = Object.values(r).reduce((a, b) => a + b, 0);
    const keys = Object.keys(REASONS).filter(k => k !== 'landline' || r.landline || S.imp?.mapping?.reject_landline);
    $('#reasons').innerHTML = keys.map(k => `
      <div class="rr"><span class="k">${k}</span><div class="track"><i style="width:${total ? ((r[k] || 0) / total) * 100 : 0}%"></i></div><span class="v">${fmt(r[k] || 0)}</span>
      <span class="h">${REASONS[k]}</span></div>`).join('');
  }

  function renderCounters() {
    const R = S.run, s = R.sums;
    const est = Math.max(S.imp?.est_rows || 0, s.rows);
    tween($('#cRows'), s.rows);
    $('#cOf').textContent = S.phase === 'done' ? 'rows' : `/ ~${fmt(est)}`;
    $('#cPct').textContent = S.phase === 'done' ? '100%' : pct(s.rows, est);
    const den = S.phase === 'done' ? Math.max(1, s.rows) : Math.max(1, est);
    $('#sbA').style.width = `${(s.ok / den) * 100}%`;
    $('#sbB').style.width = `${(s.dup / den) * 100}%`;
    $('#sbC').style.width = `${(s.bad / den) * 100}%`;
    tween($('#cOk'), s.ok); tween($('#cDup'), s.dup); tween($('#cBad'), s.bad);
    $('#mFast').textContent = s.ok + s.dup ? pct(s.fast, s.ok + s.dup) : '–';
    tick();
    renderReasons();
  }

  function tick() {
    const R = S.run;
    if (!R.claimedAt) return;
    const end = S.phase === 'processing' ? Date.now() : (R.doneAt || R.lastAt || Date.now());
    const el = Math.max(0.001, (end - R.claimedAt) / 1000);
    $('#mEl').textContent = secs(el);
    $('#mRate').textContent = R.sums.rows ? compact(R.sums.rows / el) : '–';
  }

  function log(e, html) {
    const R = S.run;
    const t = R.t0 ? (ts(e.at) - R.t0) / 1000 : 0;
    const tm = t < 60 ? `+${t.toFixed(1)}s` : `+${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, '0')}`;
    const el = document.createElement('div');
    el.className = `ll k-${e.kind}`;
    el.innerHTML = `<span class="tm">${tm}</span><span class="tx">${html}</span>`;
    const box = $('#log');
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
    box.appendChild(el);
    while (box.children.length > 400) box.firstChild.remove();
    if (stick) box.scrollTop = box.scrollHeight;
  }

  function applyEvent(e, animate, idx) {
    const R = S.run, d = e.data || {}, at = ts(e.at);
    if (R.t0 == null) R.t0 = at;
    R.lastAt = at;
    switch (e.kind) {
      case 'uploaded':
        log(e, `received <b>${bytes(d.bytes)}</b> in ${secs(d.seconds || 0)} · sha256 <b>${esc((d.sha256 || '').slice(0, 10))}…</b> · ~<b>${fmt(d.est_rows)}</b> rows`);
        break;
      case 'queued':
        R.queuedAt = at; S.phase = 'queued';
        if (d.retry) log(e, `re-queued · resumes after row <b>${fmt(d.checkpoint_row)}</b>`);
        else {
          const extra = [d.name_column ? `name <b>${esc(d.name_column)}</b>` : '', d.country_column ? `country <b>${esc(d.country_column)}</b>` : '', d.var_columns?.length ? `vars <b>${esc(d.var_columns.join(', '))}</b>` : ''].filter(Boolean).join(' · ');
          log(e, `mapping locked · phone <b>${esc((d.phone_columns || []).join(' → '))}</b>${extra ? ' · ' + extra : ''} · default <b>${esc(d.default_region)}</b> · <b>queued</b>`);
        }
        break;
      case 'claimed':
        R.claimedAt ??= at; R.worker = d.worker; R.chunkSize = d.chunk_size || R.chunkSize; R.resumeFrom = d.resume_from || 0;
        S.phase = 'processing'; R.doneAt = null;
        $('#worker').textContent = d.worker || '';
        R.total = Math.max(1, Math.ceil((S.imp?.est_rows || 0) / R.chunkSize), R.chunks.length);
        ensureCells(R.total, !animate);
        $('#chunkMeta').textContent = `${fmt(R.chunkSize)} rows each`;
        log(e, `claimed by <b>${esc(d.worker)}</b> · SKIP LOCKED · ${d.pool} validator process${d.pool === 1 ? '' : 'es'}` +
          (d.resume_from ? ` · <b>resuming after row ${fmt(d.resume_from)}</b>` : ''));
        break;
      case 'chunk': {
        d._at = at;
        R.chunks.push(d);
        const s = R.sums;
        s.rows += d.rows; s.ok += d.inserted; s.dup += d.dups; s.bad += d.rejected; s.fast += d.fast || 0;
        for (const [k, v] of Object.entries(d.reasons || {})) R.reasons[k] = (R.reasons[k] || 0) + v;
        fillCell(d, animate, idx);
        runLane(d, animate);
        log(e, `chunk <b>${d.n}</b> · rows ${fmt(d.first_row)}–${fmt(d.last_row)} · <b>+${fmt(d.inserted)}</b> new · ${fmt(d.dups)} dup · ${fmt(d.rejected)} rejected · ${ms(d.validate_ms + d.copy_ms + d.errors_ms + d.merge_ms)}`);
        break;
      }
      case 'released':
        S.phase = 'queued'; R.queuedAt = at;
        log(e, `worker stopping · lock released at row <b>${fmt(d.checkpoint_row)}</b>`);
        break;
      case 'done':
        S.phase = 'done'; R.doneAt = at;
        log(e, `done · <b>${fmt(d.checkpoint_row)}</b> rows in <b>${secs(d.seconds || 0)}</b> · ${compact((d.checkpoint_row || 0) / Math.max(.001, d.seconds || 0))} rows/s`);
        break;
      case 'analyze':
        log(e, `ANALYZE contacts · ${ms(d.ms)}`);
        break;
      case 'failed':
        S.phase = 'failed'; R.error = d.error;
        log(e, `failed · ${esc(d.error)}`);
        break;
      default:
        log(e, esc(e.kind));
    }
    if (animate) { renderCounters(); renderRail(); markLive(); renderSparks(); }
  }

  function renderRun() {
    const n = S.run.chunks.length;
    $('#chunkCount').textContent = n ? `${n} committed` : '';
    renderCounters(); renderRail(); markLive(); renderSparks();
  }

  async function play() {
    const R = S.run;
    if (R.playing) return;
    R.playing = true;
    while (R.queue.length && S.run === R) {
      const e = R.queue.shift();
      applyEvent(e, true);
      $('#chunkCount').textContent = R.chunks.length ? `${R.chunks.length} committed` : '';
      const pending = R.queue.filter(x => x.kind === 'chunk').length;
      await sleep(e.kind === 'chunk' ? Math.max(70, Math.min(420, 900 / (pending + 1))) : 160);
    }
    R.playing = false;
    if (S.run === R) { renderRun(); finalize(); }
  }

  function finalize() {
    const R = S.run;
    if (R.playing || R.queue.length) return;
    if (S.phase === 'done' && !R.finalized) {
      R.finalized = true;
      // Drop the estimated cells that never received a chunk.
      $$('#cgrid .cell').forEach(c => {
        if (+c.dataset.i >= R.chunks.length) { c.classList.add('gone'); setTimeout(() => c.remove(), 450); }
      });
      setTimeout(() => ensureCells(R.chunks.length), 460);
      $$('#cgrid .cell.live').forEach(c => c.classList.remove('live'));
      showResults();
    }
    if (S.phase === 'failed' && !R.finalized) {
      R.finalized = true;
      $('#errSlot').innerHTML = `<div class="errbox"><span>${esc(S.imp?.error || R.error || 'failed')}</span>${S.imp?.mapping ? '<button class="btn" id="retryBtn">retry</button>' : ''}</div>`;
      $('#retryBtn')?.addEventListener('click', async () => {
        try { await api(`/api/imports/${S.id}/retry`, { method: 'POST' }); $('#errSlot').innerHTML = ''; R.finalized = false; poll(); }
        catch (err) { toast(err.message, true); }
      });
    }
  }

  // ------------------------------------------------------------ polling
  function stopPoll() { clearTimeout(S.timer); S.timer = null; }

  async function poll() {
    stopPoll();
    const id = S.id;
    if (!id) return;
    try {
      const d = await api(`/api/imports/${id}?after_event=${S.lastEvent}`);
      if (id !== S.id) return;
      S.imp = d.import; S.queueInfo = d.queue;
      if (S.lockedAt !== S.imp.locked_at) { S.lockedAt = S.imp.locked_at; if (S.imp.locked_at) heartbeat(); }
      if (d.events.length) {
        S.lastEvent = d.events[d.events.length - 1].id;
        S.run.queue.push(...d.events);
        play();
      } else if (!S.run.playing) renderRail();
      if (['queued', 'processing'].includes(S.imp.status)) S.timer = setTimeout(poll, 1000);
      else finalize();
    } catch (e) {
      if (id === S.id) S.timer = setTimeout(poll, 2500);
    }
  }

  async function openImport(id) {
    stopPoll();
    S.id = id; S.lastEvent = 0; S.lockedAt = null;
    resetRun();
    let d;
    try { d = await api(`/api/imports/${id}`); }
    catch (e) { toast(e.message, true); location.hash = ''; return; }
    if (S.id !== id) return;
    S.imp = d.import; S.queueInfo = d.queue;
    crumb();
    if (S.imp.status === 'uploaded') {
      S.phase = 'uploaded';
      try { await loadPreview(); } catch (e) { toast(e.message, true); }
      renderRail();
      showView('map');
      return;
    }
    showView('run');
    const events = d.events;
    S.lastEvent = events.length ? events[events.length - 1].id : 0;
    const live = ['queued', 'processing'].includes(S.imp.status);
    // A backlog (already-finished import, or one opened mid-run) is applied at once; new events are played.
    const fresh = live ? events.filter(e => ts(e.at) > Date.now() - 4000) : [];
    events.slice(0, events.length - fresh.length).forEach((e, i) => applyEvent(e, false, i));
    renderRun();
    if (S.run.chunks.length) runLane(S.run.chunks[S.run.chunks.length - 1], false);
    if (fresh.length) { S.run.queue.push(...fresh); play(); }
    if (live) S.timer = setTimeout(poll, 600);
    else finalize();
  }

  function route() {
    const id = parseInt(location.hash.replace('#', ''), 10);
    if (id) openImport(id); else showIdle();
  }

  // ------------------------------------------------------------ results (keyset pagination)
  function showResults() {
    $('#results').classList.remove('hidden');
    S.res = { tab: 'contacts', stack: [], cursor: null, last: null, first: null };
    $$('.tab').forEach(t => t.classList.toggle('on', t.dataset.t === 'contacts'));
    loadPage('first');
  }

  async function loadPage(dir) {
    const r = S.res, imp = S.imp, lim = 50;
    const isC = r.tab === 'contacts';
    $('#statusField').classList.toggle('hidden', !isC);
    let url;
    if (dir === 'first') r.stack = [];
    if (isC) {
      const st = $('#statusSel').value;
      url = `/api/campaigns/${imp.campaign_id}/contacts?limit=${lim}${st ? `&status=${encodeURIComponent(st)}` : ''}`;
      if (dir === 'next' && r.last != null) url += `&after_id=${r.last}`;
      if (dir === 'prev' && r.first != null) url += `&before_id=${r.first}`;
      $('#exportBtn').href = `/api/campaigns/${imp.campaign_id}/contacts.csv${st ? `?status=${encodeURIComponent(st)}` : ''}`;
      $('#resMeta').textContent = `campaign ${imp.campaign_id} · +${fmt(imp.valid_rows)} from this file`;
    } else {
      url = `/api/imports/${imp.id}/errors?limit=${lim}`;
      if (dir === 'next' && r.last != null) url += `&after_row=${r.last}`;
      if (dir === 'prev' && r.first != null) url += `&before_row=${r.first}`;
      $('#exportBtn').href = `/api/imports/${imp.id}/errors.csv`;
      $('#resMeta').textContent = `${fmt(imp.invalid_rows)} rows`;
    }
    try {
      const d = await api(url);
      const items = d.items;
      if (!items.length && dir !== 'first') return;
      if (dir === 'next') r.stack.push(r.first);
      if (dir === 'prev') r.stack.pop();
      r.first = isC ? d.first_id : d.first_row;
      r.last = isC ? d.last_id : d.last_row;
      $('#prevBtn').disabled = !r.stack.length;
      $('#nextBtn').disabled = items.length < lim;
      const tbl = $('#tbl');
      if (isC) {
        tbl.innerHTML = `<thead><tr><th>id</th><th>phone_e164</th><th>name</th><th>vars</th><th>status</th></tr></thead><tbody>${items.map((c, i) => `
          <tr style="animation-delay:${i * 8}ms"><td class="m muted">${c.id}</td><td class="m">${esc(c.phone_e164)}</td><td>${esc(c.name || '')}</td>
          <td class="vars">${c.vars ? Object.entries(c.vars).map(([k, v]) => `<span>${esc(k)}</span> ${esc(v)}`).join(' &nbsp; ') : ''}</td>
          <td><span class="rs ${esc(c.status)}">${esc(c.status)}</span></td></tr>`).join('')}</tbody>`;
      } else {
        tbl.innerHTML = `<thead><tr><th>row</th><th>raw_phone</th><th>reason</th></tr></thead><tbody>${items.map((x, i) => `
          <tr style="animation-delay:${i * 8}ms"><td class="m muted">${fmt(x.row_no)}</td><td class="m">${esc(x.raw_phone ?? '')}</td><td><span class="rs ${esc(x.reason)}">${esc(x.reason)}</span></td></tr>`).join('')}</tbody>`;
      }
      if (!items.length) tbl.innerHTML = '<tbody><tr><td class="empty">nothing here</td></tr></tbody>';
    } catch (e) { toast(e.message, true); }
  }

  // ------------------------------------------------------------ tooltip
  function tipFor(c) {
    const row = (k, v) => `<div class="r"><span>${k}</span><span>${v}</span></div>`;
    return row('chunk', c.n) + row('rows', `${fmt(c.first_row)}–${fmt(c.last_row)}`) + row('new', fmt(c.inserted)) +
      row('duplicate', fmt(c.dups)) + row('rejected', fmt(c.rejected)) + row('validate', ms(c.validate_ms)) +
      row('copy + merge', ms(c.copy_ms + c.errors_ms + c.merge_ms)) + row('worker rss', `${c.rss_mb} MB`);
  }

  // ------------------------------------------------------------ wiring
  function wire() {
    const drop = $('#drop'), file = $('#file');
    drop.addEventListener('click', () => { if (!drop.classList.contains('busy')) file.click(); });
    file.addEventListener('change', () => { if (file.files[0]) upload(file.files[0]); file.value = ''; });
    ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); if (!drop.classList.contains('busy')) drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f && !drop.classList.contains('busy')) upload(f); });
    window.addEventListener('dragover', e => e.preventDefault());
    window.addEventListener('drop', e => e.preventDefault());

    $('#recent').addEventListener('click', e => { const r = e.target.closest('.ri'); if (r) location.hash = r.dataset.id; });
    $('#newBtn').addEventListener('click', () => { location.hash = ''; });

    $('#clist').addEventListener('click', e => {
      const b = e.target.closest('[data-act]'); if (!b) return;
      toggle(b.dataset.act, b.closest('.cr').dataset.h);
    });
    $('#colFilter').addEventListener('input', renderCols);
    $('#pri').addEventListener('click', e => {
      const b = e.target.closest('[data-act]'); if (!b) return;
      const h = b.closest('.pi').dataset.h, i = S.m.phones.indexOf(h);
      if (b.dataset.act === 'rm') S.m.phones.splice(i, 1);
      if (b.dataset.act === 'up' && i > 0) [S.m.phones[i - 1], S.m.phones[i]] = [S.m.phones[i], S.m.phones[i - 1]];
      renderMapping();
    });
    $('#nameTag').addEventListener('click', e => { if (e.target.closest('[data-act="rmname"]')) { S.m.name = null; renderMapping(); } });
    $('#countryTag').addEventListener('click', e => { if (e.target.closest('[data-act="rmcountry"]')) { S.m.country = null; renderMapping(); rescore(); } });
    $('#region').addEventListener('change', rescore);
    $('#varTags').addEventListener('click', e => {
      const b = e.target.closest('[data-act="rmvar"]'); if (!b) return;
      const h = b.closest('.tag').dataset.h; S.m.vars = S.m.vars.filter(v => v !== h); renderMapping();
    });
    $('#startBtn').addEventListener('click', startImport);

    const tip = $('#tip');
    $('#cgrid').addEventListener('mousemove', e => {
      const cell = e.target.closest('.cell');
      const c = cell && S.run.chunks.find(x => x.n === +cell.dataset.i + 1);
      if (!c) { tip.classList.remove('on'); return; }
      tip.innerHTML = tipFor(c);
      const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 10);
      const y = Math.min(e.clientY + 14, window.innerHeight - tip.offsetHeight - 10);
      tip.style.left = `${x}px`; tip.style.top = `${y}px`;
      tip.classList.add('on');
    });
    $('#cgrid').addEventListener('mouseleave', () => tip.classList.remove('on'));

    $$('.tab').forEach(t => t.addEventListener('click', () => {
      $$('.tab').forEach(x => x.classList.toggle('on', x === t));
      S.res.tab = t.dataset.t; S.res.first = S.res.last = null; loadPage('first');
    }));
    $('#statusSel').addEventListener('change', () => loadPage('first'));
    $('#prevBtn').addEventListener('click', () => loadPage('prev'));
    $('#nextBtn').addEventListener('click', () => loadPage('next'));

    window.addEventListener('hashchange', route);
    setInterval(() => { if (S.phase === 'processing' && S.view === 'run') tick(); }, 250);
    window.addEventListener('resize', () => { if (S.view === 'run') ensureCells($('#cgrid').children.length); });
  }

  (async () => {
    try { S.cfg = await api('/api/config'); } catch (_) { S.cfg = { chunk_size: 10000, max_upload_bytes: 300 * 1048576 }; }
    wire();
    route();
  })();
})();
