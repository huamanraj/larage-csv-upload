(() => {
  'use strict';

  // The server records what happened (import_events). The UI turns those events into a timeline of
  // steps, one per node of the flow, and plays it back at any speed. Rewinding is just picking a time.

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const fmt = n => Number(n || 0).toLocaleString('en-US');
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const bytes = b => { const u = ['B', 'KB', 'MB', 'GB']; let i = 0; b = Number(b || 0); while (b >= 1024 && i < 3) { b /= 1024; i++; } return `${b.toFixed(i && b < 100 ? 1 : 0)} ${u[i]}`; };
  const ms = v => `${v < 10 ? Number(v).toFixed(1) : Math.round(v)} ms`;
  const clock = t => { t /= 1000; return t < 60 ? `${t.toFixed(1)}s` : `${Math.floor(t / 60)}:${(t % 60).toFixed(1).padStart(4, '0')}`; };

  // Minimum on-screen time per step at 1x, so steps that take microseconds are still visible.
  const MIN_STEP = 140, MIN_EVENT = 450, MIN_PIECE = 40;
  const NODES = ['user', 'create', 'receive', 'write', 'hash', 'moreBytes', 'queued', 'push', 'pick', 'open',
    'read', 'validate', 'save', 'progress', 'moreRows', 'done'];

  const S = { id: null, events: [], lastEvent: 0, imp: null, segs: [], total: 0, t: 0, speed: 1, playing: false,
    shown: -1, timer: null, cfg: { chunk_size: 10000 } };

  async function api(url, opts) {
    const r = await fetch(url, opts);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `${r.status}`);
    return body;
  }

  let toastT;
  function toast(msg, err) {
    const t = $('#toast');
    t.textContent = msg; t.classList.toggle('err', !!err); t.classList.add('on');
    clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('on'), 3500);
  }

  // ---------------------------------------------------------------- events -> timeline segments
  function build() {
    const segs = [];
    const st = { vals: {}, seen: new Set(), loop: {}, stats: null };
    const push = (node, dur, vals = {}, extra = {}) => {
      Object.assign(st.vals, vals);
      st.seen.add(node);
      segs.push({ node, dur: Math.max(1, dur), vals: { ...st.vals }, seen: new Set(st.seen), loop: { ...st.loop },
        stats: st.stats && { ...st.stats }, ...extra });
    };
    const ev = S.events, at = e => new Date(e.at).getTime();
    let file = '', total = 0, chunksTotal = 1, prevAt = null;

    for (const e of ev) {
      const d = e.data || {};
      if (e.kind === 'created') {
        file = d.file_name; total = d.size;
        push('user', MIN_EVENT, { user: `${file} · ${bytes(total)}` });
        push('create', MIN_EVENT, { create: `#${S.id} · status uploading` });
      } else if (e.kind === 'uploaded') {
        const samples = d.samples || [];
        const pieces = Math.max(1, Math.ceil(d.bytes / 65536));
        for (let i = 0; i < samples.length; i++) {
          const [t, b, h] = samples[i];
          const dt = i ? t - samples[i - 1][0] : t;
          const q = Math.max(MIN_PIECE, dt) / 4, piece = Math.max(1, Math.ceil(b / 65536)), last = i === samples.length - 1;
          st.loop = { upload: `piece ${fmt(piece)} / ${fmt(pieces)}` };
          push('receive', q, { receive: `piece ${fmt(piece)} · 64 KB` });
          push('write', q, { write: `${bytes(b)} / ${bytes(d.bytes)} → ${d.path.split('/').pop()}` });
          push('hash', q, { hash: `sha256 ${h}…` });
          push('moreBytes', q, { moreBytes: last ? 'no' : 'yes' });
        }
        st.loop = {};
        st.vals.hash = `sha256 ${d.sha256.slice(0, 16)}…`;
        chunksTotal = Math.max(1, Math.ceil(d.est_rows / S.cfg.chunk_size));
        st.stats = { rows: 0, est: d.est_rows, valid: 0, invalid: 0, dups: 0, inserted: 0 };
      } else if (e.kind === 'queued') {
        push('queued', MIN_EVENT, { queued: 'status queued' });
        push('push', MIN_EVENT, { push: `import_id ${S.id} · waiting for a worker` });
        prevAt = at(e);
      } else if (e.kind === 'claimed') {
        const wait = prevAt ? at(e) - prevAt : 0;
        push('pick', Math.min(1500, Math.max(MIN_EVENT, wait)), {
          push: `import_id ${S.id} · waited ${clock(wait)}`,
          pick: `${d.worker} · SKIP LOCKED${d.resume_from ? ` · resume after row ${fmt(d.resume_from)}` : ''}` });
      } else if (e.kind === 'opened') {
        push('open', MIN_EVENT, { open: `${d.path.split('/').pop()} · ${bytes(d.bytes)}` });
      } else if (e.kind === 'chunk') {
        chunksTotal = Math.max(chunksTotal, d.n);
        st.loop = { chunk: `chunk ${d.n} / ${chunksTotal}` };
        push('read', Math.max(MIN_STEP, d.read_ms), { read: `rows ${fmt(d.first_row)}–${fmt(d.last_row)} · ${ms(d.read_ms)}` });
        push('validate', Math.max(MIN_STEP, d.validate_ms), { validate: `${fmt(d.valid)} valid · ${fmt(d.invalid)} invalid · ${ms(d.validate_ms)}` });
        const s = st.stats || { rows: 0, est: 0, valid: 0, invalid: 0, dups: 0, inserted: 0 };
        st.stats = { ...s, rows: s.rows + d.rows, inserted: s.inserted + d.inserted, invalid: s.invalid + d.invalid, dups: s.dups + d.dups };
        push('save', Math.max(MIN_STEP, d.save_ms), { save: `+${fmt(d.inserted)} new · ${fmt(d.dups)} duplicate · ${ms(d.save_ms)}` });
        push('progress', Math.max(MIN_STEP, d.progress_ms), { progress: `checkpoint row ${fmt(d.last_row)}` });
        push('moreRows', MIN_STEP, { moreRows: 'yes' });
      } else if (e.kind === 'done') {
        if (segs.length && segs[segs.length - 1].node === 'moreRows') {
          segs[segs.length - 1].vals.moreRows = 'no';
          st.vals.moreRows = 'no';
        }
        st.loop = {};
        push('done', MIN_EVENT * 2, { done: `${fmt(d.checkpoint_row)} rows in ${d.seconds}s · ${fmt(d.valid_rows)} saved` });
      } else if (e.kind === 'failed') {
        st.loop = {};
        push('done', MIN_EVENT * 2, { done: `failed: ${d.error}` }, { fail: true });
      } else if (e.kind === 'released') {
        push('queued', MIN_EVENT, { queued: `status queued again · checkpoint row ${fmt(d.checkpoint_row)}` });
        prevAt = at(e);
      }
    }
    let acc = 0;
    for (const s of segs) { s.start = acc; acc += s.dur; }
    S.segs = segs;
    S.total = acc;
  }

  // ---------------------------------------------------------------- render one moment
  function segAt(t) {
    const a = S.segs;
    let lo = 0, hi = a.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (a[mid].start <= t) lo = mid; else hi = mid - 1; }
    return lo;
  }

  function render() {
    const i = S.segs.length ? segAt(S.t) : -1;
    if (i !== S.shown) {
      S.shown = i;
      const seg = S.segs[i];
      for (const n of NODES) {
        const el = $(`.node[data-n="${n}"]`);
        el.classList.toggle('active', !!seg && seg.node === n && !seg.fail);
        el.classList.toggle('fail', !!seg && seg.node === n && !!seg.fail);
        el.classList.toggle('seen', !!seg && seg.seen.has(n));
        $('.v', el).textContent = seg?.vals[n] ?? '';
      }
      for (const l of ['upload', 'chunk']) {
        const box = $(`.loop[data-l="${l}"]`);
        box.classList.toggle('active', !!seg?.loop[l]);
        $('.it', box).textContent = seg?.loop[l] ?? '';
      }
      $('.loop[data-l="queue"]').classList.toggle('active', seg?.node === 'push');
      const s = seg?.stats;
      $('#stats').innerHTML = s ? [
        `rows <b>${fmt(s.rows)}</b> / ~${fmt(s.est)}`,
        `<span class="ok">saved <b>${fmt(s.inserted)}</b></span>`,
        `<span class="dup">duplicate <b>${fmt(s.dups)}</b></span>`,
        `<span class="bad">invalid <b>${fmt(s.invalid)}</b></span>`,
      ].join('') : '';
    }
    const p = S.total ? (S.t / S.total) * 100 : 0;
    const sc = $('#scrub');
    if (!sc.dragging) sc.value = Math.round(p * 10);
    sc.style.setProperty('--p', `${p}%`);
    $('#time').textContent = `${clock(S.t)} / ${clock(S.total)}`;
  }

  // ---------------------------------------------------------------- playback
  const finished = () => ['done', 'failed'].includes(S.imp?.status);
  let lastFrame = 0;
  function frame(now) {
    const dt = lastFrame ? now - lastFrame : 0;
    lastFrame = now;
    if (S.playing) {
      S.t = Math.min(S.total, S.t + dt * S.speed);
      if (S.t >= S.total && finished()) setPlaying(false);  // reached the end of a finished import
    }
    render();
    requestAnimationFrame(frame);
  }

  function setPlaying(on) {
    if (on && S.t >= S.total && finished()) { S.t = 0; S.shown = -1; }
    S.playing = on;
    $('#icPlay').classList.toggle('hidden', on);
    $('#icPause').classList.toggle('hidden', !on);
  }

  // ---------------------------------------------------------------- data
  async function poll() {
    clearTimeout(S.timer);
    const id = S.id;
    if (!id) return;
    try {
      const d = await api(`/api/imports/${id}?after_event=${S.lastEvent}`);
      if (id !== S.id) return;
      S.imp = d.import;
      if (d.events.length) {
        S.events.push(...d.events);
        S.lastEvent = d.events[d.events.length - 1].id;
        build();
        S.shown = -1;
      }
      if (!finished()) S.timer = setTimeout(poll, 700);
    } catch (e) {
      if (id === S.id) S.timer = setTimeout(poll, 2000);
    }
  }

  async function open(id) {
    S.id = id; S.events = []; S.lastEvent = 0; S.segs = []; S.total = 0; S.shown = -1; S.t = 0;
    await poll();
    if (!S.imp) return;
    $('#dropT').textContent = `#${id} · ${S.imp.file_name}`;
    $('#dropS').textContent = 'drop another csv to start a new import';
    // A finished import opens on its final state; a running one plays from the start and follows along.
    const live = !finished();
    S.t = live ? 0 : S.total;
    setPlaying(live);
  }

  function upload(file) {
    const max = S.cfg.max_upload_bytes || Infinity;
    if (file.size > max) { toast(`${file.name} is ${bytes(file.size)}; limit is ${bytes(max)}`, true); return; }
    const drop = $('#drop');
    drop.classList.add('busy');
    $('#dropT').textContent = file.name;
    $('#dropS').textContent = 'uploading…';
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/imports');
    xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name));
    xhr.setRequestHeader('Content-Type', 'text/csv');
    xhr.upload.onprogress = e => {
      const p = e.lengthComputable ? e.loaded / e.total : 0;
      $('#dropBar').style.width = `${p * 100}%`;
      $('#dropS').textContent = `uploading ${bytes(e.loaded)} / ${bytes(e.total)}`;
    };
    const reset = () => { drop.classList.remove('busy'); $('#dropBar').style.width = '0'; };
    xhr.onerror = () => { reset(); toast('upload failed', true); };
    xhr.onload = () => {
      reset();
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* not json */ }
      if (xhr.status !== 200 && xhr.status !== 202) {
        toast(body.detail || `upload failed (${xhr.status})`, true);
        $('#dropT').textContent = 'drop a csv';
        $('#dropS').textContent = 'columns: phone, name, country · others kept as vars';
        return;
      }
      if (body.duplicate) toast(`same file already imported as #${body.import_id}`);
      location.hash = body.import_id;
    };
    xhr.send(file);
  }

  // ---------------------------------------------------------------- wiring
  function wire() {
    const drop = $('#drop'), file = $('#file');
    file.addEventListener('change', () => { if (file.files[0]) upload(file.files[0]); file.value = ''; });
    ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if (f && !drop.classList.contains('busy')) upload(f); });
    drop.addEventListener('click', e => { if (drop.classList.contains('busy')) e.preventDefault(); });

    $('#playBtn').addEventListener('click', () => setPlaying(!S.playing));
    $$('#speeds button').forEach(b => b.addEventListener('click', () => {
      S.speed = Number(b.dataset.s);
      $$('#speeds button').forEach(x => x.classList.toggle('on', x === b));
    }));
    const sc = $('#scrub');
    let wasPlaying = false;
    sc.addEventListener('pointerdown', () => { sc.dragging = true; wasPlaying = S.playing; S.playing = false; });
    sc.addEventListener('input', () => { S.t = (sc.value / 1000) * S.total; });
    const release = () => { if (sc.dragging) { sc.dragging = false; setPlaying(wasPlaying); } };
    sc.addEventListener('pointerup', release);
    sc.addEventListener('change', release);
    document.addEventListener('keydown', e => {
      if (e.target.tagName === 'INPUT' && e.target.type !== 'range') return;
      if (e.code === 'Space') { e.preventDefault(); setPlaying(!S.playing); }
      if (e.code === 'ArrowLeft') S.t = Math.max(0, S.t - 1000);
      if (e.code === 'ArrowRight') S.t = Math.min(S.total, S.t + 1000);
    });
    window.addEventListener('hashchange', route);
  }

  function route() {
    const id = parseInt(location.hash.slice(1), 10);
    if (id) open(id);
  }

  (async () => {
    try { S.cfg = await api('/api/config'); } catch (_) { /* defaults */ }
    wire();
    route();
    requestAnimationFrame(frame);
  })();
})();
