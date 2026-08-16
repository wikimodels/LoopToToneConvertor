const $ = (id) => document.getElementById(id);

let state = null;
let filter = 'all';
let lastLogSeq = 0;

const STATUS_LABEL = {
  pending: 'в очереди',
  working: 'в работе',
  done: 'готово',
  failed: 'ошибка',
};

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

function fmtClock(ts) {
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 8);
}

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function control(action) {
  return api('/api/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  }).catch(e => alert('Ошибка: ' + e.message));
}

function setBusy(btn, busy) {
  btn.disabled = busy;
}

async function refresh() {
  try {
    state = await api('/api/state');
    render();
  } catch (e) {
    $('apiBadge').className = 'badge bad';
    $('apiBadge').textContent = 'Сервер недоступен';
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
  badge.textContent = 'API: ' + (state.api_ok === null ? '...' : state.api_ok ? 'доступен' : 'недоступен');

  const run = $('btnRun'), pause = $('btnPause'), resume = $('btnResume');
  run.textContent = state.running ? 'Стоп' : 'Запустить';
  run.classList.toggle('danger', !!state.running);
  pause.disabled = !state.running || state.paused;
  resume.disabled = !state.running || !state.paused;
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

const STATUSHINT = { pending: '', working: 'работаем…', done: 'готово', failed: '' };

function renderFiles() {
  const tbody = $('filesTable').querySelector('tbody');
  const files = state.files.filter(f => filter === 'all' || f.status === filter || (filter === 'done' && f.status === 'done'));
  tbody.innerHTML = '';
  $('filesEmpty').style.display = state.files.length ? 'none' : 'block';
  for (const f of files) {
    const tr = document.createElement('tr');
    tr.className = f.status;
    const r = f.result || {};
    const step = f.status === 'working'
      ? '<span class="spin"></span>' + (f.step || 'подготовка')
      : '<span class="stepdone">' + (f.step || '') + '</span>';
    const actions = f.json_file
      ? `<button data-dl="${f.json_file}" title="Скачать">⬇</button>
         <button data-open="${f.json_file}" title="Открыть файл">📂</button>`
      : '';
    const error = f.error ? `<div class="err" title="${f.error.replace(/"/g, '&quot;')}">${f.error.length > 120 ? f.error.slice(0, 120) + '…' : f.error}</div>` : '';
    tr.innerHTML =
      `<td class="fname" title="${f.name}">${f.name}
         <div class="fsize">${fmtBytes(f.size)}${f.result && f.result.duration != null ? ' · ' + f.result.duration + 'с' : ''}</div>
       </td>
       <td><span class="chip ${f.status}">${STATUS_LABEL[f.status] || f.status}</span>
         ${f.attempts > 1 && f.status === 'failed' ? `<div class="fsize">попыток: ${f.attempts}</div>` : ''}
         ${error}</td>
       <td>${step}</td>
       <td>${r.bpm || ''}</td>
       <td>${r.key ? r.key + (r.scale ? ' ' + r.scale : '') : ''}</td>
       <td>${r.time_signature ? r.time_signature + '/4' : ''}</td>
       <td>${r.steps || ''}</td>
       <td>${r.notes || ''}</td>
       <td class="prog" title="${(r.progression || []).join(' ')}">${(r.progression || []).join(' ')}</td>
       <td class="jsoncell">${f.json_file ? `<a class="link" href="/api/file/${encodeURIComponent(f.json_file)}" download="${f.json_file}">${f.json_file}</a>` : ''}</td>
       <td class="acts">${actions}</td>`;
    tbody.appendChild(tr);
  }
}

function renderLog() {
  if (!state.log || !state.log.length) return;
  const box = $('logBox');
  const needScroll = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  for (const e of state.log) {
    if (e.n <= lastLogSeq) continue;
    const div = document.createElement('div');
    div.className = 'logline ' + e.level;
    const time = new Date(e.t * 1000).toTimeString().slice(0, 8);
    div.textContent = `[${time}] ${e.msg}`;
    box.appendChild(div);
  }
  lastLogSeq = state.log[state.log.length - 1].n;
  while (box.childElementCount > 600) box.firstElementChild.remove();
  if (needScroll) box.scrollTop = box.scrollHeight;
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
    a.href = '/api/file/' + encodeURIComponent(t.dataset.dl);
    a.download = t.dataset.dl;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } else if (t.dataset.open) {
    api('/api/open-file?name=' + encodeURIComponent(t.dataset.open)).catch(() => {});
  }
});

refresh();
setInterval(refresh, 1000);