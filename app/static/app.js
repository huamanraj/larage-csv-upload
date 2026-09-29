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
    validate: ['Validate rows', 'Each phone is cleaned to +E.164 across CPU cores. The first valid phone column wins; empty, invalid and Excel 9.19E+11 values are rejected.',
      'The calling layer needs dialable numbers in one format, and one format is what makes duplicates detectable.'],
    save: ['Save chunk to contacts', 'Valid rows are COPYed into a temp table, then INSERT … ON CONFLICT DO NOTHING into contacts. Rejects go to import_errors with a reason.',
      'COPY is about 10× faster than row inserts, and the unique index guarantees one contact per phone per campaign.'],
    progress: ['Save checkpoint + progress', 'checkpoint_row and the counters are updated in the same transaction as the saved rows.',
      'Data and checkpoint commit together or not at all, so after a crash nothing is saved twice or lost.'],
    moreRows: ['More rows?', 'Loop until the end of the file; the worker lease is refreshed on every chunk.',
      'A fresh lease tells other workers this one is alive, so they do not take the job over.'],
    res_cpu: ['CPU', 'Cores busy during each step: the API process while it receives the upload, then the worker plus its validation processes for each chunk\'s read, validate and save.',
      'Validation is the CPU-heavy part and spreads across cores − 1, so the API stays free. During save the worker mostly waits on the database, which shows as a dip here and a spike in DB writes.'],
    res_ram: ['RAM', 'Memory held by the API during upload, and by the worker with its validation processes during processing.',
      'It should stay flat whatever the file size, because only one piece or one chunk is in memory at a time. A line that keeps climbing would mean a leak.'],
    res_db: ['DB writes', 'Write-ahead log (WAL) the database writes for each chunk\'s transaction: the new contacts, rejected rows, index entries and checkpoint.',
      'This is the database\'s disk-write load, arriving in bursts once per chunk. A larger CHUNK_SIZE makes fewer, bigger spikes; a smaller one makes more, smaller ones.'],
    done: ['Import row status done', 'Status becomes done and the final counts stay on the imports row.',
      'Progress and totals are read from this one row, never by counting millions of contacts.'],
  };

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
    const st = { vals: {}, seen: new Set(), loop: {}, stats: null, db: null };
    const push = (node, dur, vals = {}, extra = {}) => {
      Object.assign(st.vals, vals);
      st.seen.add(node);
      segs.push({ node, dur: Math.max(1, dur), vals: { ...st.vals }, seen: new Set(st.seen), loop: { ...st.loop },
        stats: st.stats && { ...st.stats }, db: st.db, ...extra });
    };
    // m = resource usage during a segment: who (api | worker), cores used, RSS MB; wal = MB written by the DB.
    const use = (who, cpu, rss) => (cpu == null ? {} : { m: { who, cpu, rss } });
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
        st.loop = {};
        st.vals.hash = `sha256 ${d.sha256.slice(0, 16)}…`;
        chunksTotal = Math.max(1, Math.ceil(d.est_rows / S.cfg.chunk_size));
        st.stats = { rows: 0, est: d.est_rows, valid: 0, invalid: 0, dups: 0, inserted: 0 };
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
        push('open', MIN_EVENT, { open: `${d.path.split('/').pop()} · ${bytes(d.bytes)}` });
      } else if (e.kind === 'chunk') {
        chunksTotal = Math.max(chunksTotal, d.n);
        st.loop = { chunk: `chunk ${d.n} / ${chunksTotal}` };
        push('read', Math.max(MIN_STEP, d.read_ms), { read: `rows ${fmt(d.first_row)}–${fmt(d.last_row)} · ${ms(d.read_ms)}` },
          use('worker', d.cpu_read, d.rss_mb));
        push('validate', Math.max(MIN_STEP, d.validate_ms), { validate: `${fmt(d.valid)} valid · ${fmt(d.invalid)} invalid · ${ms(d.validate_ms)}` },
          use('worker', d.cpu_validate, d.rss_mb));
        const s = st.stats || { rows: 0, est: 0, valid: 0, invalid: 0, dups: 0, inserted: 0 };
        st.stats = { ...s, rows: s.rows + d.rows, inserted: s.inserted + d.inserted, invalid: s.invalid + d.invalid, dups: s.dups + d.dups };
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
  }

  function renderCharts(seg) {
    const cores = S.cores || S.cfg.cores || 1, pk = S.peaks || {};
    const m = seg?.m, db = seg?.db;
    const cpuEl = $('.rc[data-c="cpu"] .rv'), ramEl = $('.rc[data-c="ram"] .rv'), dbEl = $('.rc[data-c="db"] .rv');
    cpuEl.textContent = m ? `${m.who} ${m.cpu.toFixed(1)} / ${cores} cores` : (pk.cpu ? `peak ${pk.cpu.toFixed(1)} / ${cores} cores` : '');
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

  function wire() {
    wireInfo();
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
