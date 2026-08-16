const $ = (id) => document.getElementById(id);

const API = 'http://127.0.0.1:8002';

let state = null;
let filter = 'all';
let lastLogSeq = 0;

const STATUS_LABEL = { pending: 'в очереди', working: 'в работе', done: 'готово', failed: 'ошибка' };

function fmtBytes(n) {
  if (!n) return '';
  if (n > 1048576) return (n / 1048576).toFixed(1) + ' МБ';
  if (n > 1024) return (n / 1024).toFixed(0) + ' КБ';
  return n + ' Б';
}

function fmtEta(sec) {
  if (sec == null) return '—';
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m > 0 ? `${m}м ${s}с` : `${s}с`;
}

async function api(url, opts) {
  const r = await fetch(API + url, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function control(action) {
  return api('/api/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  }).catch(() => {});
}

async function refresh() {
  try {
    state = await api('/api/state');
    render();
  } catch (e) {
    $('apiBadge').className = 'badge bad';
    $('apiBadge').textContent = 'движок: недоступен';
    $('acBadge').className = 'badge unknown';
    $('acBadge').textContent = 'AppCheck: —';
  }
}

function render() {
  const t = state.totals;
  $('stTotal').textContent = t.total;
  $('stDone').textContent = t.done;
  $('stWork').textContent = t.working;
  $('stQueued').textContent = t.pending;
  $('stFailed').textContent = t.failed;
  $('stEta').textContent = fmtEta(state.eta);

  const pct = t.total ? Math.round(t.done / t.total * 100) : 0;
  $('barFill').style.width = pct + '%';
  $('barFill').textContent = pct > 0 ? pct + '%' : '';

  const badge = $('apiBadge');
  badge.className = 'badge ' + (state.api_ok ? 'ok' : 'bad');
  badge.textContent = 'движок: ' + (state.api_ok === null ? '...' : state.api_ok ? 'ок' : 'недоступен');

  const ac = state.appcheck;
  const acBadge = $('acBadge');
  if (ac && ac.present) {
    acBadge.className = 'badge ok';
    const left = Math.round((ac.expires_in_sec != null ? ac.expires_in_sec : 0) / 3600);
    acBadge.textContent = `AppCheck: ок (${left}ч)`;
  } else {
    acBadge.className = 'badge bad';
    acBadge.textContent = ac && ac.error ? 'AppCheck: нет токена' : 'AppCheck: ...';
  }

  const run = $('btnRun');
  run.textContent = state.running ? 'Стоп' : 'Запустить';
  run.classList.toggle('danger', !!state.running);
  $('btnPause').disabled = !state.running || state.paused;
  $('btnResume').disabled = !state.running || !state.paused;
  $('btnRetry').disabled = t.failed === 0;

  const work = state.files.find(f => f.status === 'working');
  if (work) {
    $('currentInfo').textContent = `Выполняется: ${work.name} → ${work.step || 'подготовка'}`;
  } else if (state.running && !state.paused) {
    $('currentInfo').textContent = 'Обработка активна, ожидание следующего файла (лимит API)…';
  } else if (state.paused) {
    $('currentInfo').textContent = 'Пауза';
  } else if (t.total && t.done + t.failed === t.total) {
    $('currentInfo').textContent = 'Очередь завершена';
  } else {
    $('currentInfo').textContent = 'ничего не выполняется';
  }

  fillSettings();
  renderFiles();
  renderLog();
}

function fillSettings() {
  const c = state.config;
  if (document.activeElement !== $('setSource')) $('setSource').value = c.source;
  if (document.activeElement !== $('setOutput')) $('setOutput').value = c.output;
  $('setApi').value = c.api_base;
  $('setBeatModel').value = c.beat_model;
  $('setChordModel').value = c.chord_model;
  $('setInterval').value = c.call_interval_sec;
  $('setDetectKey').value = String(!!c.detect_key);
}

function renderFiles() {
  const list = $('filesList');
  const files = state.files.filter(f => filter === 'all' || f.status === filter);
  list.innerHTML = '';
  $('filesEmpty').style.display = state.files.length ? 'none' : 'block';
  for (const f of files) {
    const r = f.result || {};
    const step = f.status === 'working'
      ? '<span class="spin"></span>' + (f.step || 'подготовка')
      : '<span class="stepdone">' + (f.step || '') + '</span>';
    let facts = '';
    if (r.bpm) facts += `<span>♪ ${r.bpm}</span>`;
    if (r.key) facts += `<span>${r.key}${r.scale ? ' ' + r.scale : ''}</span>`;
    if (r.notes != null) facts += `<span>нот: ${r.notes}</span>`;
    if (r.chords != null) facts += `<span>акк: ${r.chords}</span>`;
    if (r.progression && r.progression.length) {
      facts += `<span class="prog" title="${r.progression.join(' ')}">${r.progression.join(' ')}</span>`;
    }
    let acts = '';
    if (f.json_file) {
      acts += `<button data-dl="${f.json_file}" title="Скачать JSON">⬇</button>`;
      acts += `<button data-open="${f.json_file}" title="Открыть JSON">📂</button>`;
    }
    if (f.midi_file) {
      acts += `<button data-mdl="${f.midi_file}" title="Скачать MIDI">🎹</button>`;
      acts += `<button data-openm="${f.midi_file}" title="Открыть MIDI">📁</button>`;
    }
    const error = f.error
      ? `<div class="err" title="${f.error.replace(/"/g, '&quot;')}">${f.error.length > 110 ? f.error.slice(0, 110) + '…' : f.error}</div>`
      : '';
    const div = document.createElement('div');
    div.className = 'fitem ' + f.status;
    div.innerHTML =
      `<div class="fitem-top">
         <span class="fname" title="${f.name}">${f.name}</span>
         <span class="chip ${f.status}">${STATUS_LABEL[f.status] || f.status}</span>
         ${acts ? `<span class="facts">${acts}</span>` : ''}
       </div>
       <div class="fmeta"><span>${fmtBytes(f.size)}</span>${facts}${step}</div>${error}`;
    list.appendChild(div);
  }
}

function renderLog() {
  if (!state.log || !state.log.length) return;
  const box = $('logBox');
  for (const e of state.log) {
    if (e.n <= lastLogSeq) continue;
    const div = document.createElement('div');
    div.className = 'logline ' + e.level;
    const time = new Date(e.t * 1000).toTimeString().slice(0, 8);
    div.textContent = `[${time}] ${e.msg}`;
    box.appendChild(div);
    if (box.childElementCount > 1000) box.firstElementChild.remove();
  }
  lastLogSeq = state.log[state.log.length - 1].n;
  box.scrollTop = box.scrollHeight;
}

async function saveSettings() {
  $('settingsMsg').textContent = 'сохраняю…';
  const body = {
    source: $('setSource').value.trim(),
    output: $('setOutput').value.trim(),
    api_base: $('setApi').value.trim(),
    beat_model: $('setBeatModel').value,
    chord_model: $('setChordModel').value,
    call_interval_sec: parseInt($('setInterval').value, 10) || 33,
    detect_key: $('setDetectKey').value === 'true',
  };
  try {
    const r = await api('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    $('settingsMsg').textContent = r.ok ? 'сохранено ✓' : 'ошибка: ' + (r.error || '');
  } catch (e) {
    $('settingsMsg').textContent = 'ошибка: ' + e.message;
  }
}

document.addEventListener('click', (e) => {
  const t = e.target;
  if (t.id === 'btnRun') control(state.running ? 'stop' : 'start');
  else if (t.id === 'btnPause') control('pause');
  else if (t.id === 'btnResume') control('resume');
  else if (t.id === 'btnRescan') control('rescan');
  else if (t.id === 'btnRetry') control('retry-failed');
  else if (t.id === 'btnOpenOut') api('/api/open-output').catch(() => {});
  else if (t.id === 'btnOpenMidi') api('/api/open-midi').catch(() => {});
  else if (t.id === 'btnOpenSrc') api('/api/open-source').catch(() => {});
  else if (t.id === 'btnSaveSettings') saveSettings();
  else if (t.id === 'btnClearLog') { $('logBox').innerHTML = ''; lastLogSeq = 0; }
  else if (t.dataset.f) {
    document.querySelectorAll('.filters button').forEach(b => b.classList.remove('on'));
    t.classList.add('on');
    filter = t.dataset.f;
    renderFiles();
  } else if (t.dataset.dl) {
    const a = document.createElement('a');
    a.href = API + '/api/file/' + encodeURIComponent(t.dataset.dl);
    a.download = t.dataset.dl;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } else if (t.dataset.open) {
    api('/api/open-file?name=' + encodeURIComponent(t.dataset.open)).catch(() => {});
  } else if (t.dataset.mdl) {
    const a = document.createElement('a');
    a.href = API + '/api/file/' + encodeURIComponent(t.dataset.mdl);
    a.download = t.dataset.mdl;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } else if (t.dataset.openm) {
    api('/api/open-file?name=' + encodeURIComponent(t.dataset.openm)).catch(() => {});
  }
});

refresh();
setInterval(refresh, 1000);