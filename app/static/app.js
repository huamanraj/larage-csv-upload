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

  // What each step does and why it matters, shown by the (i) button on the step.
  const INFO = {
    user: ['User uploads CSV', 'The browser sends the file as the raw request body, not a multipart form.',
      'The server can stream it straight to disk instead of buffering a 250 MB form in memory.'],
    create: ['Create import row', 'An imports row is inserted with status uploading before any byte is stored.',
      'The user gets an id at once, and an abandoned or failed upload is visible instead of lost.'],
    upload: ['Upload stream loop', 'Receive, write and hash repeat for every piece until the body ends.',
      'Memory stays at one piece (~64 KB) whether the file is 1 MB or 300 MB.'],
    receive: ['Receive next piece', 'The server reads the next piece of the request body.',
      'Only one small piece is in memory at a time, so large uploads never exhaust RAM.'],
    write: ['Write piece to local storage', 'The piece is appended to /data/imports/{id}.csv.',
      'The file lives on disk, not in memory or the database. The worker re-reads it later, and switching to S3 changes only this step.'],
    hash: ['Update sha256', 'The piece is fed into a running sha256 of the whole file.',
      'A fingerprint for free, with no second read. Uploading the same file to the same campaign again returns the existing import instead of importing twice.'],
    moreBytes: ['More bytes?', 'Loop until the body ends; the size cap is checked on every piece.',
      'An oversized upload is stopped mid-stream (413) instead of after filling the disk.'],
    queued: ['Import row status queued', 'Once the file is stored and has a phone column, the row becomes queued.',
      'A job is only visible to workers when its file is complete, so a worker never reads half a file.'],
    queue: ['Job queue · Postgres', 'status = queued in the imports table is the queue. Only the import_id is passed on.',
      'No Redis or Celery to run. The job and its data share one database, so progress and data can commit together. Celery or SQS could replace it without other changes.'],
    push: ['Push import_id', 'The job becomes claimable by any worker.',
      'Only a small id is queued; the file stays in storage and the state stays in the imports row.'],
    pick: ['Worker picks up import_id', 'UPDATE … FOR UPDATE SKIP LOCKED claims the oldest queued import and sets a lease (worker_id, locked_at).',
      'Two workers never take the same job. If a worker dies, its lease goes stale and another worker resumes the job.'],
    open: ['Open file as a stream', 'The stored CSV is opened as a text stream and the phone, name and country columns are detected.',
      'The file is never loaded whole, so worker memory stays flat for 1k or 1M rows.'],
    chunk: ['Chunk loop', 'Read, validate and save repeat for every 10,000 rows until the file ends.',
      'Each chunk is a small, self-contained transaction: bounded memory, steady progress, and a crash only redoes one chunk.'],
    read: ['Read next rows', 'The next 10,000 rows are read; rows up to the checkpoint are skipped on a resume.',
      'Chunks bound memory and keep each database write small.'],
    validate: ['Validate rows', 'Each phone is cleaned to +E.164 across CPU cores; the first valid phone column wins. A row is rejected when no number is usable: empty, too_short (< 5 digits), sci_notation (Excel 9.19E+11), invalid for its country, or junk (9999999999). E-mail and birthday columns are checked too: a bad value is blanked, the row is kept.',
      'The calling layer needs dialable numbers in one format, and one format is what makes duplicates detectable. Cheap checks run first, so most rows never reach the slower phone parser.'],
    save: ['Save chunk to contacts', 'Valid rows are COPYed into a temp table, then INSERT … ON CONFLICT DO NOTHING into contacts. Rejects go to import_errors with a reason.',
      'COPY is about 10× faster than row inserts, and the unique index guarantees one contact per phone per campaign.'],
    progress: ['Save checkpoint + progress', 'checkpoint_row and the counters are updated in the same transaction as the saved rows.',
      'Data and checkpoint commit together or not at all, so after a crash nothing is saved twice or lost.'],
    moreRows: ['More rows?', 'Loop until the end of the file; the worker lease is refreshed on every chunk.',
      'A fresh lease tells other workers this one is alive, so they do not take the job over.'],
    cores: ['Core cap', 'Limits the worker to the first N CPU cores (Linux CPU affinity) and validates each chunk in N parallel slices. auto uses every core for the worker, validating in cores − 1 slices.',
      'Run the same file at 1, 2, 4… cores to see how latency scales. Validation speeds up with more cores; reading and the database save do not. Postgres itself is not capped.'],
    runs: ['Imports', 'Recent imports with live progress, results, processing time and throughput. waiting means another import of the same campaign is running. re-run processes a stored file again with the core cap selected above.',
      'Each re-run goes into a fresh campaign, so every run does identical work (no duplicates from the previous run) and the times are directly comparable.'],
    campaign: ['Campaign', 'own per file: every file goes into its own campaign, like files from different customers; they are processed in parallel (up to the worker\'s slots). shared: every file goes into campaign 1; they are processed one after another.',
      'Two imports into the same campaign would race on its duplicate check, so the queue never runs them together. Different campaigns share nothing, so they can run side by side.'],
    system: ['System', 'CPU and memory of the worker, the API and Postgres, sampled every 0.5 s, plus the database\'s write rate (WAL/s) and one bar per import on the same time axis. Hover to read any moment.',
      'Shows what parallel imports cost: CPU climbs with each extra import until the cores are full, then imports slow each other down; memory stays flat because each import holds one chunk at a time. Under Docker, Postgres runs in another container, so its CPU shows as part of "db + other".'],
    res_cpu: ['CPU', 'Cores busy during each step: the API process while it receives the upload, then the worker plus its validation processes for each chunk\'s read, validate and save. When several imports run at once, the worker figure covers all of them.',
      'Validation is the CPU-heavy part and spreads across cores − 1, so the API stays free. During save the worker mostly waits on the database, which shows as a dip here and a spike in DB writes.'],
    res_ram: ['RAM', 'Memory held by the API during upload, and by the worker with its validation processes during processing.',
      'It should stay flat whatever the file size, because only one piece or one chunk is in memory at a time. A line that keeps climbing would mean a leak.'],
    res_db: ['DB writes', 'Write-ahead log (WAL) the database writes for each chunk\'s transaction: the new contacts, rejected rows, index entries and checkpoint.',
      'This is the database\'s disk-write load, arriving in bursts once per chunk. A larger CHUNK_SIZE makes fewer, bigger spikes; a smaller one makes more, smaller ones.'],
    done: ['Import row status done', 'Status becomes done and the final counts stay on the imports row.',
      'Progress and totals are read from this one row, never by counting millions of contacts.'],
  };

  const S = { id: null, events: [], lastEvent: 0, imp: null, segs: [], total: 0, t: 0, speed: 1, playing: false,
    shown: -1, timer: null, cfg: { chunk_size: 10000 }, cap: null, camp: 'own', uploads: new Map() };
  try { S.cap = JSON.parse(localStorage.getItem('coresCap')) || null; } catch (_) { /* private mode */ }
  try { S.camp = localStorage.getItem('campMode') === 'shared' ? 'shared' : 'own'; } catch (_) { /* private mode */ }

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
    const st = { vals: {}, seen: new Set(), loop: {}, stats: null, db: null };
    const push = (node, dur, vals = {}, extra = {}) => {
      Object.assign(st.vals, vals);
      st.seen.add(node);
      segs.push({ node, dur: Math.max(1, dur), vals: { ...st.vals }, seen: new Set(st.seen), loop: { ...st.loop },
        stats: st.stats && { ...st.stats }, db: st.db, ...extra });
    };
    // m = resource usage during a segment: who (api | worker), cores used, RSS MB; wal = MB written by the DB.
    const use = (who, cpu, rss) => (cpu == null ? {} : { m: { who, cpu, rss, running: st.stats?.running } });
    const ev = S.events, at = e => new Date(e.at).getTime();
    S.cores = null;
    let file = '', total = 0, chunksTotal = 1, prevAt = null;

    for (const e of ev) {
      const d = e.data || {};
      if (e.kind === 'created') {
        file = d.file_name; total = d.size;
        push('user', MIN_EVENT, { user: d.rerun_of ? `re-run of #${d.rerun_of} · ${file}` : `${file} · ${bytes(total)}` });
        push('create', MIN_EVENT, { create: `#${S.id} · status uploading` });
      } else if (e.kind === 'uploaded') {
        const samples = d.samples || [];
        const pieces = Math.max(1, Math.ceil(d.bytes / 65536));
        for (let i = 0; i < samples.length; i++) {
          const [t, b, h, cpu, rss] = samples[i];
          const m = use('api', cpu, rss);
          const dt = i ? t - samples[i - 1][0] : t;
          const q = Math.max(MIN_PIECE, dt) / 4, piece = Math.max(1, Math.ceil(b / 65536)), last = i === samples.length - 1;
          st.loop = { upload: `piece ${fmt(piece)} / ${fmt(pieces)}` };
          push('receive', q, { receive: `piece ${fmt(piece)} · 64 KB` }, m);
          push('write', q, { write: `${bytes(b)} / ${bytes(d.bytes)} → ${d.path.split('/').pop()}` }, m);
          push('hash', q, { hash: `sha256 ${h}…` }, m);
          push('moreBytes', q, { moreBytes: last ? 'no' : 'yes' }, m);
        }
        if (d.rerun_of) push('write', MIN_EVENT, { write: `reusing stored ${d.path.split('/').pop()} · ${bytes(d.bytes)}` });
        st.loop = {};
        st.vals.hash = `sha256 ${d.sha256.slice(0, 16)}…`;
        chunksTotal = Math.max(1, Math.ceil(d.est_rows / S.cfg.chunk_size));
        st.stats = { rows: 0, est: d.est_rows, valid: 0, invalid: 0, dups: 0, inserted: 0, reasons: {}, email: 0, date: 0 };
      } else if (e.kind === 'queued') {
        push('queued', MIN_EVENT, { queued: 'status queued' });
        push('push', MIN_EVENT, { push: `import_id ${S.id} · waiting for a worker` });
        prevAt = at(e);
      } else if (e.kind === 'claimed') {
        if (d.cores) S.cores = d.cores;
        const wait = prevAt ? at(e) - prevAt : 0;
        push('pick', Math.min(1500, Math.max(MIN_EVENT, wait)), {
          push: `import_id ${S.id} · waited ${clock(wait)}`,
          pick: `${d.worker} · SKIP LOCKED${d.resume_from ? ` · resume after row ${fmt(d.resume_from)}` : ''}` });
      } else if (e.kind === 'opened') {
        if (d.cores_used) S.cores = d.cores_used;
        const lim = !d.cores_used ? '' : !d.cap ? ` · all ${d.cores_used} cores`
          : d.pinned === false ? ` · ${d.cores_used} validation slices` : ` · capped to ${d.cores_used} of ${S.cfg.cpus || d.cores_used} cores`;
        const chk = Object.entries(d.checked || {}).filter(([, v]) => v.length).map(([k]) => k).join(' + ');
        push('open', MIN_EVENT, { open: `${d.path.split('/').pop()} · ${bytes(d.bytes)}${lim}${chk ? ` · checks ${chk}` : ''}` });
      } else if (e.kind === 'chunk') {
        chunksTotal = Math.max(chunksTotal, d.n);
        st.loop = { chunk: `chunk ${d.n} / ${chunksTotal}` };
        push('read', Math.max(MIN_STEP, d.read_ms), { read: `rows ${fmt(d.first_row)}–${fmt(d.last_row)} · ${ms(d.read_ms)}` },
          use('worker', d.cpu_read, d.rss_mb));
        const fixed = (d.email_blanked || 0) + (d.date_blanked || 0);
        push('validate', Math.max(MIN_STEP, d.validate_ms), { validate: `${fmt(d.valid)} valid · ${fmt(d.invalid)} invalid${fixed ? ` · ${fmt(fixed)} fields blanked` : ''} · ${ms(d.validate_ms)}` },
          use('worker', d.cpu_validate, d.rss_mb));
        const s = st.stats || { rows: 0, est: 0, valid: 0, invalid: 0, dups: 0, inserted: 0, reasons: {}, email: 0, date: 0 };
        const reasons = { ...(s.reasons || {}) };
        for (const [k, v] of Object.entries(d.reasons || {})) reasons[k] = (reasons[k] || 0) + v;
        st.stats = { ...s, rows: s.rows + d.rows, inserted: s.inserted + d.inserted, invalid: s.invalid + d.invalid, dups: s.dups + d.dups,
          reasons, email: (s.email || 0) + (d.email_blanked || 0), date: (s.date || 0) + (d.date_blanked || 0), running: d.running };
        if (d.wal_mb != null) st.db = { wal: d.wal_mb, ms: d.save_ms + d.progress_ms, n: d.n };
        const saving = use('worker', d.cpu_save, d.rss_mb);
        push('save', Math.max(MIN_STEP, d.save_ms), { save: `+${fmt(d.inserted)} new · ${fmt(d.dups)} duplicate · ${ms(d.save_ms)}` },
          { ...saving, wal: d.wal_mb });
        push('progress', Math.max(MIN_STEP, d.progress_ms), { progress: `checkpoint row ${fmt(d.last_row)}` }, saving);
        push('moreRows', MIN_STEP, { moreRows: 'yes' }, saving);
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
    drawCharts();
  }

  // ---------------------------------------------------------------- resource charts
  // Step lines over the same timeline as the scrubber: x = segment start/end, y = value during it.
  const W = 1000, H = 44;
  function series(pick, max, filled) {
    let d = '', open = false, x0 = 0;
    const X = t => ((t / (S.total || 1)) * W).toFixed(1), Y = v => (H - 1 - (Math.min(v, max) / max) * (H - 4)).toFixed(1);
    const close = end => { if (open) { d += filled ? `L${end},${H}L${x0},${H}Z` : ''; open = false; } };
    for (const s of S.segs) {
      const v = pick(s);
      if (v == null) { close(X(s.start)); continue; }
      const a = X(s.start), b = X(s.start + s.dur), y = Y(v);
      if (!open) { d += `M${a},${filled ? H : y}${filled ? `L${a},${y}` : ''}`; x0 = a; open = true; } else d += `L${a},${y}`;
      d += `L${b},${y}`;
    }
    close(X(S.total));
    return d;
  }

  function drawCharts() {
    const segs = S.segs, cores = S.cores || S.cfg.cores || 1;
    const who = w => s => (s.m?.who === w ? s.m : null);
    const rssMax = Math.max(64, ...segs.map(s => s.m?.rss || 0)) * 1.2;
    const walMax = Math.max(0.5, ...segs.map(s => s.wal || 0)) * 1.2;
    for (const w of ['api', 'worker']) {
      $(`.rc[data-c="cpu"] path.${w}`).setAttribute('d', series(s => who(w)(s)?.cpu, cores, true));
      $(`.rc[data-c="ram"] path.${w}`).setAttribute('d', series(s => who(w)(s)?.rss, rssMax, true));
    }
    $('.rc[data-c="db"] path.db').setAttribute('d', series(s => (s.wal != null ? s.wal : null), walMax, true));
    S.peaks = {
      cpu: Math.max(0, ...segs.map(s => s.m?.cpu || 0)), rss: Math.max(0, ...segs.map(s => s.m?.rss || 0)),
      wal: segs.reduce((a, s) => a + (s.wal || 0), 0),
    };
    // Imports recorded before resource tracking existed have steps but no numbers.
    S.noMetrics = segs.some(s => s.node === 'read') && !segs.some(s => s.m || s.wal != null);
  }

  function renderCharts(seg) {
    const cores = S.cores || S.cfg.cores || 1, pk = S.peaks || {};
    const m = seg?.m, db = seg?.db;
    const cpuEl = $('.rc[data-c="cpu"] .rv'), ramEl = $('.rc[data-c="ram"] .rv'), dbEl = $('.rc[data-c="db"] .rv');
    if (S.noMetrics) {
      const msg = 'not recorded for this import · upload a new file';
      [cpuEl, ramEl, dbEl].forEach(el => { el.textContent = msg; el.title = 'This import ran before CPU/RAM tracking was added, or the worker could not read it.'; });
      return;
    }
    const who = m && m.who === 'worker' && m.running > 1 ? `worker (${m.running} imports)` : m?.who;
    cpuEl.textContent = m ? `${who} ${m.cpu.toFixed(1)} / ${cores} cores` : (pk.cpu ? `peak ${pk.cpu.toFixed(1)} / ${cores} cores` : '');
    cpuEl.title = `peak ${(pk.cpu || 0).toFixed(2)} of ${cores} cores`;
    ramEl.textContent = m ? `${m.who} ${fmt(Math.round(m.rss))} MB` : (pk.rss ? `peak ${fmt(Math.round(pk.rss))} MB` : '');
    ramEl.title = `peak ${fmt(Math.round(pk.rss || 0))} MB`;
    dbEl.textContent = db ? `#${db.n} · ${db.wal.toFixed(1)} MB · ${ms(db.ms)}` : (pk.wal ? `total ${pk.wal.toFixed(1)} MB` : '');
    dbEl.title = `chunk WAL / DB time · total ${(pk.wal || 0).toFixed(1)} MB for this import`;
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
      renderCharts(seg);
      const s = seg?.stats;
      const why = Object.entries(s?.reasons || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k.replace('_', ' ')} ${fmt(v)}`).join(' · ');
      $('#stats').innerHTML = s ? [
        `rows <b>${fmt(s.rows)}</b> / ~${fmt(s.est)}`,
        `<span class="ok">saved <b>${fmt(s.inserted)}</b></span>`,
        `<span class="dup">duplicate <b>${fmt(s.dups)}</b></span>`,
        `<span class="bad">invalid <b>${fmt(s.invalid)}</b>${why ? ` (${esc(why)})` : ''}</span>`,
        s.email || s.date ? `blanked <b>${fmt(s.email)}</b> e-mails · <b>${fmt(s.date)}</b> dates` : '',
      ].join('') : '';
    }
    const p = S.total ? (S.t / S.total) * 100 : 0;
    const sc = $('#scrub');
    if (!sc.dragging) sc.value = Math.round(p * 10);
    sc.style.setProperty('--p', `${p}%`);
    const cx = (p * 10).toFixed(1);
    $$('.rc .cur').forEach(l => { l.setAttribute('x1', cx); l.setAttribute('x2', cx); });
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
    $$('#runsBody tr').forEach(tr => tr.classList.toggle('cur', Number(tr.dataset.id) === id));
    await poll();
    if (!S.imp) return;
    if (!S.uploads.size) {
      $('#dropT').textContent = `#${id} · ${S.imp.file_name}`;
      $('#dropS').textContent = 'drop more csv files to start new imports';
    }
    // A finished import opens on its final state; a running one plays from the start and follows along.
    const live = !finished();
    S.t = live ? 0 : S.total;
    setPlaying(live);
  }

  // Several files upload at once; each becomes its own import (and, in "own per file" mode, its own campaign),
  // so the worker processes them in parallel.
  function uploadOne(file) {
    return new Promise(resolve => {
      const xhr = new XMLHttpRequest();
      const q = new URLSearchParams();
      if (S.camp === 'shared') q.set('campaign_id', '1');
      if (S.cap) q.set('cores', S.cap);
      xhr.open('POST', `/api/imports${q.toString() ? `?${q}` : ''}`);
      xhr.setRequestHeader('X-File-Name', encodeURIComponent(file.name));
      xhr.setRequestHeader('Content-Type', 'text/csv');
      const u = { loaded: 0, total: file.size };
      S.uploads.set(file.name, u);
      xhr.upload.onprogress = e => { u.loaded = e.loaded; u.total = e.total || file.size; uploadProgress(); };
      const end = body => { S.uploads.delete(file.name); uploadProgress(); resolve(body); };
      xhr.onerror = () => { toast(`${file.name}: upload failed`, true); end(null); };
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText); } catch (_) { /* not json */ }
        if (xhr.status !== 200 && xhr.status !== 202) { toast(`${file.name}: ${body.detail || `upload failed (${xhr.status})`}`, true); end(null); return; }
        if (body.duplicate) toast(`${file.name}: same file already imported as #${body.import_id}`);
        end(body);
      };
      xhr.send(file);
    });
  }

  function uploadProgress() {
    const list = [...S.uploads.values()];
    const drop = $('#drop');
    drop.classList.toggle('busy', list.length > 0);
    if (!list.length) { $('#dropBar').style.width = '0'; return; }
    const loaded = list.reduce((a, u) => a + u.loaded, 0), total = list.reduce((a, u) => a + u.total, 0) || 1;
    $('#dropBar').style.width = `${(loaded / total) * 100}%`;
    $('#dropT').textContent = list.length > 1 ? `uploading ${list.length} files` : [...S.uploads.keys()][0];
    $('#dropS').textContent = `${bytes(loaded)} / ${bytes(total)}`;
    renderRuns();
  }

  async function upload(files) {
    const max = S.cfg.max_upload_bytes || Infinity;
    const ok = [...files].filter(f => {
      if (f.size > max) toast(`${f.name} is ${bytes(f.size)}; limit is ${bytes(max)}`, true);
      return f.size <= max && !S.uploads.has(f.name);
    });
    if (!ok.length) return;
    const results = await Promise.all(ok.map(uploadOne));
    const ids = results.filter(Boolean).map(r => r.import_id);
    if (!S.uploads.size) {
      $('#dropT').textContent = 'drop csv files';
      $('#dropS').textContent = 'one or several at once · columns: phone, name, country, email, birthday …';
    }
    if (ids.length > 1) toast(`${ids.length} files queued · ${S.camp === 'shared' ? 'same campaign: one after another' : 'own campaigns: processed in parallel'}`);
    if (ids.length) location.hash = Math.min(...ids);
    pollMonitor();
  }

  // ---------------------------------------------------------------- system timeline (live)
  // Samples come from the api and worker every 0.5 s. CPU and RAM are stacked per process group; DB writes is
  // the WAL rate. Below, one bar per import on the same time axis: who ran when, and what it cost.
  const M = { worker: [], api: [], lastId: 0, imports: [], offset: 0, hover: null, timer: null, range: [0, 1] };
  const KEEP = 15 * 60e3, SW = 1000, SH = 56;

  async function pollMonitor() {
    if (M.busy) { M.again = true; return; }  // a request is in flight: refresh right after it
    clearTimeout(M.timer);
    M.busy = true;
    try {
      const d = await api(`/api/monitor?after=${M.lastId}&window=900&limit=12`);
      M.offset = d.now - Date.now();
      for (const x of d.samples) {
        (x.source === 'api' ? M.api : M.worker).push({ t: x.t, d: x.data });
        M.lastId = Math.max(M.lastId, x.id);
      }
      for (const arr of [M.worker, M.api]) while (arr.length && arr[0].t < d.now - KEEP) arr.shift();
      M.imports = d.imports;
      renderRuns();
      renderSystem();
    } catch (_) { /* server restarting: keep the last picture */ }
    M.busy = false;
    M.timer = setTimeout(pollMonitor, M.again ? 0 : 1000);
    M.again = false;
  }

  const active = i => ['uploading', 'queued', 'processing'].includes(i.status);

  // The run to show: the latest group of imports that overlap in time (a batch dropped together).
  function runGroup() {
    const now = Date.now() + M.offset;
    const imps = M.imports.filter(i => i.created_ms).sort((a, b) => a.created_ms - b.created_ms);
    const group = [];
    let end = -Infinity;
    for (const i of imps) {
      const e = i.finished_ms || now;
      if (i.created_ms > end + 5000) group.length = 0;
      group.push(i);
      end = Math.max(end, e);
    }
    return { group, end, now };
  }

  function windowRange() {
    const { group, end, now } = runGroup();
    if (!group.length) return { start: now - 60e3, end: now, group, live: true };
    const live = group.some(active) || now - end < 2000;
    const start = Math.max(Math.min(...group.map(i => i.created_ms)) - 3000, now - KEEP);
    return live ? { start: Math.min(start, now - 20e3), end: now, group, live }
                : { start, end: end + 3000, group, live };
  }

  // Nearest api sample for each worker sample (both are sorted by time).
  function aligned(start, end) {
    const W = M.worker.filter(x => x.t >= start - 1000 && x.t <= end + 1000);
    const out = [];
    let j = 0;
    for (let i = 0; i < W.length; i++) {
      const w = W[i];
      while (j + 1 < M.api.length && Math.abs(M.api[j + 1].t - w.t) <= Math.abs(M.api[j].t - w.t)) j++;
      const a = M.api[j] && Math.abs(M.api[j].t - w.t) < 1500 ? M.api[j].d : null;
      const p = W[i - 1];
      const dt = p ? (w.t - p.t) / 1000 : 0;
      const pg = w.d.pg, pp = p?.d.pg;
      out.push({
        t: w.t, cores: w.d.cores, running: (w.d.running || []).length,
        cpu: { worker: w.d.self.cpu, api: a ? a.self.cpu : 0, db: w.d.db ? w.d.db.cpu : null, machine: w.d.machine.cpu },
        mem: { worker: w.d.self.mem, api: a ? a.self.mem : 0, db: w.d.db ? w.d.db.mem : null,
          used: w.d.machine.mem_used, total: w.d.machine.mem_total },
        wal: pg && pp && dt > 0 ? Math.max(0, (pg.wal - pp.wal) / 2 ** 20 / dt) : 0,
        ins: pg && pp && dt > 0 ? Math.max(0, (pg.ins - pp.ins) / dt) : 0,
      });
    }
    return out;
  }

  // Stacked areas: layers = [[key, value(x)], ...] bottom to top, clipped at max.
  function stack(pts, layers, max, start, end) {
    const X = t => (((t - start) / (end - start || 1)) * SW).toFixed(1);
    const Y = v => (SH - (Math.min(v, max) / max) * (SH - 2)).toFixed(1);
    const base = pts.map(() => 0);
    return layers.map(([cls, f]) => {
      if (!pts.length) return '';
      const top = pts.map((p, i) => base[i] + Math.max(0, f(p) || 0));
      let d = `M${X(pts[0].t)},${Y(base[0])}`;
      pts.forEach((p, i) => { d += `L${X(p.t)},${Y(top[i])}`; });
      for (let i = pts.length - 1; i >= 0; i--) d += `L${X(pts[i].t)},${Y(base[i])}`;
      top.forEach((v, i) => { base[i] = v; });
      return `<path class="f-${cls}" d="${d}Z"/>`;
    }).join('');
  }

  const grid = `<line class="gl" x1="0" y1="${SH / 2}" x2="${SW}" y2="${SH / 2}"/>`;
  const at = (pts, t) => {
    if (!pts.length) return null;
    let best = pts[pts.length - 1];
    if (t != null) for (const p of pts) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
    return best;
  };
  const gb = mb => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`);

  function renderSystem() {
    const { start, end, group, live } = windowRange();
    M.range = [start, end];
    const pts = aligned(start, end).filter(p => p.t >= start && p.t <= end);
    const cores = pts[0]?.cores || S.cfg.cores || 1;
    const dbSeen = pts.some(p => p.cpu.db != null);

    // CPU: worker + api + db (when visible) + the rest of the machine
    const cpuL = [['worker', p => p.cpu.worker], ['api', p => p.cpu.api]];
    if (dbSeen) cpuL.push(['db', p => p.cpu.db]);
    cpuL.push(['rest', p => p.cpu.machine - p.cpu.worker - p.cpu.api - (p.cpu.db || 0)]);
    $('.tl[data-c="cpu"] svg').innerHTML = grid + stack(pts, cpuL, cores, start, end);

    // RAM: what the app's processes hold (PSS); the machine total is in the readout
    const ramL = [['worker', p => p.mem.worker], ['api', p => p.mem.api]];
    if (dbSeen) ramL.push(['db', p => p.mem.db]);
    const ramMax = Math.max(256, ...pts.map(p => p.mem.worker + p.mem.api + (p.mem.db || 0))) * 1.25;
    $('.tl[data-c="ram"] svg').innerHTML = grid + stack(pts, ramL, ramMax, start, end);

    // DB writes: WAL MB/s
    const walMax = Math.max(4, ...pts.map(p => p.wal)) * 1.2;
    $('.tl[data-c="db"] svg').innerHTML = grid + stack(pts, [['db', p => p.wal]], walMax, start, end);

    // one bar per import: thin = uploading / waiting in the queue, thick = processing
    const X = t => `${Math.max(0, Math.min(100, ((t - start) / (end - start || 1)) * 100)).toFixed(3)}%`;
    const W = (a, b) => `${Math.max(0.3, ((Math.min(b, end) - Math.max(a, start)) / (end - start || 1)) * 100).toFixed(3)}%`;
    const now = Date.now() + M.offset;
    $('#gantt').innerHTML = group.slice(-10).map(i => {
      const s0 = i.started_ms, f = i.finished_ms || now;
      const bars = [];
      if (i.status === 'uploading') bars.push(`<i class="gb up" style="left:${X(i.created_ms)};width:${W(i.created_ms, now)}"></i>`);
      else bars.push(`<i class="gb q" style="left:${X(i.created_ms)};width:${W(i.created_ms, s0 || now)}"></i>`);
      if (s0) bars.push(`<i class="gb ${i.status === 'processing' ? 'run' : i.status}" style="left:${X(s0)};width:${W(s0, f)}"></i>`);
      const rate = i.elapsed > 0 ? ` · ${fmt(Math.round(i.rows / i.elapsed))} rows/s` : '';
      return `<div class="grow${i.id === S.id ? ' cur' : ''}" data-id="${i.id}" title="#${i.id} ${esc(i.file_name)} · campaign ${i.campaign_id} · ${i.status}${rate}">
        <div class="gl-l">#${i.id} ${esc(i.file_name || '')}</div><div class="gt">${bars.join('')}</div></div>`;
    }).join('') || '<div class="sys-empty">drop one or more csv files to see them run side by side</div>';

    // time axis: seconds from the start of the window
    const span = end - start, fit = Math.max(2, Math.floor(($('#axis').clientWidth || 600) / 80));
    const step = [1e3, 2e3, 5e3, 10e3, 15e3, 30e3, 60e3, 120e3, 300e3].find(x => span / x <= fit) || 600e3;
    const ticks = [];
    for (let t = 0; t <= span; t += step) ticks.push(`<span style="left:${((t / span) * 100).toFixed(2)}%">${t ? `+${clock(t)}` : clock(0)}</span>`);
    $('#axis').innerHTML = ticks.join('');

    // header: what ran, how long, at what rate, what it peaked at
    if (group.length) {
      const rows = group.reduce((a, i) => a + (i.rows || 0), 0);
      const t0 = Math.min(...group.map(i => i.started_ms || i.created_ms));
      const t1 = group.some(active) ? now : Math.max(...group.map(i => i.finished_ms || now));
      const run = pts.filter(p => p.t >= t0 && p.t <= t1);
      const pk = f => Math.max(0, ...run.map(f));
      const runningNow = group.filter(i => i.status === 'processing').length, queued = group.filter(i => i.status === 'queued').length;
      $('#sysSub').innerHTML = `${live ? '<b>live</b> · ' : 'last run · '}${group.length} import${group.length > 1 ? 's' : ''}` +
        (live && (runningNow || queued) ? ` (${runningNow} running${queued ? `, ${queued} waiting` : ''})` : '') +
        ` · ${fmt(rows)} rows in <b>${clock(t1 - t0)}</b>` + (t1 > t0 ? ` · <b>${fmt(Math.round(rows / ((t1 - t0) / 1000)))}</b> rows/s` : '') +
        (run.length ? ` · peak CPU <b>${pk(p => p.cpu.machine).toFixed(1)}</b>/${cores} · peak worker RAM <b>${gb(pk(p => p.mem.worker))}</b>` : '');
    } else $('#sysSub').textContent = `idle · ${S.cfg.slots || 1} parallel slot${(S.cfg.slots || 1) > 1 ? 's' : ''}`;
    renderReadouts(pts);
  }

  // Readouts for the hovered moment (else the latest sample): a headline value and each layer's share.
  function renderReadouts(pts) {
    const p = at(pts, M.hover);
    const set = (c, head, keys) => {
      $(`.tl[data-c="${c}"] .tl-v`).innerHTML = head;
      $(`.tl[data-c="${c}"] .lg`).innerHTML = keys.map(([cls, label, v]) =>
        `<span><i class="k ${cls}"></i>${label}${v != null ? ` <b>${v}</b>` : ''}</span>`).join('');
    };
    if (!p) { ['cpu', 'ram', 'db'].forEach(c => set(c, '<span>no samples yet</span>', [])); return; }
    const c = p.cpu, m = p.mem, db = c.db != null;
    const rest = Math.max(0, c.machine - c.worker - c.api - (c.db || 0));
    set('cpu', `${c.machine.toFixed(1)} <span>/ ${p.cores} cores</span>`, [
      ['worker', 'worker', c.worker.toFixed(1)], ['api', 'api', c.api.toFixed(1)],
      ...(db ? [['db', 'db', c.db.toFixed(1)], ['rest', 'other', rest.toFixed(1)]] : [['rest', 'db + other', rest.toFixed(1)]])]);
    set('ram', `${gb(m.worker + m.api + (m.db || 0))} <span>/ ${gb(m.total)}</span>`, [
      ['worker', 'worker', gb(m.worker)], ['api', 'api', gb(m.api)], ...(db ? [['db', 'db', gb(m.db)]] : []),
      ['rest', 'machine used', gb(m.used)]]);
    set('db', `${p.wal.toFixed(1)} <span>MB/s WAL</span>`, [
      ['db', 'rows inserted/s', fmt(Math.round(p.ins))], ['rest', 'imports running', p.running]]);
  }

  function wireSystem() {
    const body = $('#sysBody'), line = $('#hover');
    body.addEventListener('mousemove', e => {
      const g = $('.tl-g', body).getBoundingClientRect(), b = body.getBoundingClientRect();
      if (e.clientX < g.left || e.clientX > g.right) { M.hover = null; line.classList.remove('on'); renderSystem(); return; }
      const [start, end] = M.range;
      M.hover = start + ((e.clientX - g.left) / g.width) * (end - start);
      line.style.left = `${e.clientX - b.left}px`;
      line.classList.add('on');
      renderReadouts(aligned(start, end).filter(p => p.t >= start && p.t <= end));
    });
    body.addEventListener('mouseleave', () => { M.hover = null; line.classList.remove('on'); renderSystem(); });
    $('#gantt').addEventListener('click', e => { const r = e.target.closest('.grow'); if (r) location.hash = r.dataset.id; });
  }

  // ---------------------------------------------------------------- wiring
  // ---------------------------------------------------------------- core cap + runs
  const CAPS = [null, 1, 2, 4, 6, 8];
  function renderCoreSeg() {
    const cpus = S.cfg.cpus || 64;
    $('#coreSeg').innerHTML = CAPS.map(c => `<button type="button" data-c="${c ?? ''}" class="${(S.cap ?? null) === c ? 'on' : ''}"
      ${c && c > cpus ? `disabled title="only ${cpus} CPUs available"` : ''}>${c ?? 'auto'}</button>`).join('');
    $$('#runsBody .rr').forEach(b => { b.textContent = `re-run · ${S.cap ?? 'auto'}`; });
  }

  function renderRuns() {
    const rows = M.imports;
    $('#runs').classList.toggle('hidden', !rows.length);
    const running = rows.filter(r => r.status === 'processing').length;
    $('#runsSub').textContent = rows.length ? `${running} of ${S.cfg.slots || 1} slots busy` : '';
    $('#runsBody').innerHTML = rows.map(r => {
      const secs = r.seconds != null ? Number(r.seconds) : null, el = Number(r.elapsed || 0);
      const up = r.status === 'uploading' ? S.uploads.get(r.file_name) : null;
      const p = r.status === 'done' ? 1 : up ? up.loaded / (up.total || 1) : r.est_rows ? Math.min(1, r.rows / r.est_rows) : 0;
      const bar = r.status === 'uploading' ? 'up' : r.status === 'done' ? 'done' : r.status === 'failed' ? 'failed' : '';
      const why = Object.entries(r.reasons || {}).map(([k, v]) => `${k} ${fmt(v)}`).join(', ');
      const cs = r.chunk_stats || {};
      const tip = `invalid: ${why || 'none'} · blanked e-mails ${fmt(cs.email_blanked)}, dates ${fmt(cs.date_blanked)} · ${fmt(cs.chunks)} chunks`;
      const status = r.blocked_by ? `<span class="wait">waiting for #${r.blocked_by}</span>` : esc(r.status);
      const t = secs != null ? `${secs.toFixed(2)} s` : r.status === 'processing' ? `${el.toFixed(1)} s` : '–';
      const rate = secs ? r.rows / secs : r.status === 'processing' && el > 0 ? r.rows / el : 0;
      return `<tr data-id="${r.id}" class="${r.id === S.id ? 'cur' : ''}" title="${esc(tip)}">
        <td>${r.id}</td><td class="f" title="${esc(r.file_name)}">${esc(r.file_name || '')}</td>
        <td class="n">${r.campaign_id}</td>
        <td><div class="pg"><div class="pg-b"><i class="${bar}" style="width:${(p * 100).toFixed(1)}%"></i></div><span class="pg-t">${Math.round(p * 100)}%</span></div></td>
        <td class="n ok">${fmt(r.valid_rows)}</td><td class="n dup">${fmt(r.duplicate_rows)}</td><td class="n bad">${fmt(r.invalid_rows)}</td>
        <td class="n t">${t}</td><td class="n">${rate ? fmt(Math.round(rate)) : '–'}</td>
        <td class="st-${esc(r.status)}">${status}</td>
        <td>${r.status === 'done' ? `<button class="rr" type="button" data-rerun="${r.id}">re-run · ${S.cap ?? 'auto'}</button>` : ''}</td></tr>`;
    }).join('');
  }

  async function rerun(id) {
    try {
      const r = await api(`/api/imports/${id}/rerun${S.cap ? `?cores=${S.cap}` : ''}`, { method: 'POST' });
      toast(`re-running #${id} as #${r.import_id} on ${S.cap ? `${S.cap} core${S.cap > 1 ? 's' : ''}` : 'auto cores'}`);
      location.hash = r.import_id;
      pollMonitor();
    } catch (e) { toast(e.message, true); }
  }

  function wireInfo() {
    const mk = key => {
      const b = document.createElement('button');
      b.className = 'info';
      b.type = 'button';
      b.textContent = 'i';
      b.dataset.info = key;
      b.setAttribute('aria-label', `About: ${INFO[key][0]}`);
      return b;
    };
    $$('.node[data-n]').forEach(n => INFO[n.dataset.n] && n.appendChild(mk(n.dataset.n)));
    $$('.loop[data-l]').forEach(l => INFO[l.dataset.l] && $('.lh', l).appendChild(mk(l.dataset.l)));

    const pop = $('#pop');
    let openBtn = null;
    const close = () => { pop.classList.remove('on'); openBtn?.classList.remove('on'); openBtn = null; };
    document.addEventListener('click', e => {
      const b = e.target.closest('.info');
      if (!b) { if (!e.target.closest('#pop')) close(); return; }
      e.preventDefault();
      if (openBtn === b) { close(); return; }
      close();
      const [title, what, why] = INFO[b.dataset.info];
      $('.pt', pop).textContent = title;
      $('.pw', pop).innerHTML = `<b>What</b>${esc(what)}`;
      $('.py', pop).innerHTML = `<b>Why it matters</b>${esc(why)}`;
      const r = b.getBoundingClientRect(), w = Math.min(300, window.innerWidth - 32);
      const left = Math.max(16, Math.min(r.right - w, window.innerWidth - w - 16));
      pop.style.left = `${left}px`;
      pop.style.top = '0px';
      pop.classList.add('on');
      const h = pop.offsetHeight;
      pop.style.top = `${r.bottom + 8 + h > window.innerHeight - 8 ? Math.max(8, r.top - h - 8) : r.bottom + 8}px`;
      b.classList.add('on');
      openBtn = b;
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
    window.addEventListener('resize', close);
    window.addEventListener('scroll', close, { passive: true });
  }

  async function resetDb() {
    if (!confirm('Delete ALL imports, contacts and uploaded files?\n\nUse this to re-test the same CSV from scratch.')) return;
    const b = $('#resetBtn');
    b.disabled = true;
    try {
      const r = await api('/api/reset', { method: 'POST' });
      clearTimeout(S.timer);
      Object.assign(S, { id: null, imp: null, events: [], lastEvent: 0, segs: [], total: 0, t: 0, shown: -2 });  // -2 forces a redraw
      setPlaying(false);
      history.replaceState(null, '', location.pathname);
      $('#dropT').textContent = 'drop csv files';
      $('#dropS').textContent = 'one or several at once · columns: phone, name, country, email, birthday …';
      drawCharts();
      toast(`database reset · ${r.files_removed} file(s) removed`);
      pollMonitor();
    } catch (e) { toast(e.message, true); }
    b.disabled = false;
  }

  function renderCampSeg() {
    $$('#campSeg button').forEach(b => b.classList.toggle('on', b.dataset.m === S.camp));
  }

  function wire() {
    wireInfo();
    wireSystem();
    renderCoreSeg();
    renderCampSeg();
    $('#campSeg').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      S.camp = b.dataset.m;
      try { localStorage.setItem('campMode', S.camp); } catch (_) { /* private mode */ }
      renderCampSeg();
    });
    $('#coreSeg').addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b || b.disabled) return;
      S.cap = b.dataset.c ? Number(b.dataset.c) : null;
      try { localStorage.setItem('coresCap', JSON.stringify(S.cap)); } catch (_) { /* private mode */ }
      renderCoreSeg();
    });
    $('#runsBody').addEventListener('click', e => {
      const b = e.target.closest('[data-rerun]');
      if (b) { rerun(Number(b.dataset.rerun)); return; }
      const tr = e.target.closest('tr[data-id]');
      if (tr) location.hash = tr.dataset.id;
    });
    pollMonitor();
    const rb = $('#resetBtn');
    rb.classList.toggle('hidden', S.cfg.allow_reset === false);
    rb.addEventListener('click', resetDb);
    const drop = $('#drop'), file = $('#file');
    file.addEventListener('change', () => { if (file.files.length) upload([...file.files]); file.value = ''; });
    ['dragenter', 'dragover'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach(t => drop.addEventListener(t, e => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', e => { if (e.dataTransfer.files.length) upload([...e.dataTransfer.files]); });

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
