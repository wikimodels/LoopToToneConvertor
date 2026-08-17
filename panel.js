const $ = (id) => document.getElementById(id);

const API = 'http://127.0.0.1:8002';

let state = null;
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

function setServerUi(alive, busy) {
  const el = $('srvStatus');
  const btn = $('btnStartServer');
  if (alive) {
    el.textContent = 'сервер: работает';
    btn.disabled = true;
    btn.textContent = 'Запустить сервер';
    $('srvHint').classList.add('hidden');
  } else {
    btn.disabled = busy;
    btn.textContent = busy ? 'запускаю…' : 'Запустить сервер';
    el.textContent = busy ? 'сервер: запускаю…' : 'сервер: не запущен';
  }
}

async function pingEngine() {
  try {
    await api('/api/state');
    return true;
  } catch (e) {
    return false;
  }
}

function copyText(txt) {
  try {
    navigator.clipboard.writeText(txt);
  } catch (e) { /* ignore */ }
}

async function ensureServer() {
  if (await pingEngine()) {
    setServerUi(true, false);
    return true;
  }
  setServerUi(false, true);
  let resp;
  try {
    resp = await chrome.runtime.sendMessage({ type: 'start-server' });
  } catch (e) {
    resp = { ok: false, error: String(e && e.message || e) };
  }
  if (!resp || !resp.ok) {
    setServerUi(false, false);
    const hint = $('srvHint');
    hint.classList.remove('hidden');
    hint.innerHTML = `Хост не смог запустить движок. Если host не установлен, выполните в командной строке (из папки проекта):<br><code>install_host.cmd ${chrome.runtime.id}</code><br><button id="btnCopyCmd">Скопировать команду</button><span id="srvHintMsg"></span>`;
    $('btnCopyCmd').addEventListener('click', () => copyText(`install_host.cmd ${chrome.runtime.id}`));
    return false;
  }
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (await pingEngine()) {
      setServerUi(true, false);
      $('srvHint').classList.add('hidden');
      return true;
    }
    await new Promise(r => setTimeout(r, 800));
  }
  setServerUi(false, false);
  $('srvHint').classList.remove('hidden');
  $('srvHint').textContent = 'Движок не ответил за 20 секунд. Посмотрите server.log.';
  return false;
}

async function refresh() {
  try {
    state = await api('/api/state');
    setServerUi(true, false);
    render();
  } catch (e) {
    setServerUi(false, false);
    $('apiBadge').className = 'badge bad';
    $('apiBadge').textContent = 'движок: не запущен';
    $('acBadge').className = 'badge unknown';
    $('acBadge').textContent = 'AppCheck: —';
  }
}

function render() {
  const t = state.totals;
  $('stTotal').textContent = t.total;
  $('stDone').textContent = t.done;
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

  fillSettings();
  renderFiles();
  renderLog();
}

function fillSettings() {
  const c = state.config;
  if (document.activeElement !== $('setSource')) $('setSource').value = c.source;
  if (document.activeElement !== $('setOutput')) $('setOutput').value = c.output;
  if (document.activeElement !== $('setRawDir')) $('setRawDir').value = c.raw_dir || '';
  $('setApi').value = c.api_base;
  $('setBeatModel').value = c.beat_model;
  $('setChordModel').value = c.chord_model;
  $('setInterval').value = c.call_interval_sec;
  $('setDetectKey').value = String(!!c.detect_key);
}

function renderFiles() {
  const list = $('filesList');
  const files = state.files;
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

async function clearResults(raw) {
  const what = raw ? 'Raw' : 'JSON';
  if (!confirm(`Удалить все файлы из папки ${what}?`)) return;
  try {
    const r = await api('/api/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [raw ? 'raw' : 'output']: true }),
    });
    refresh();
    if (!r.ok) alert('Ошибка очистки: ' + (r.error || ''));
  } catch (e) {
    alert('Ошибка очистки: ' + e.message);
  }
}

async function saveSettings() {
  $('settingsMsg').textContent = 'сохраняю…';
  const body = {
    source: $('setSource').value.trim(),
    output: $('setOutput').value.trim(),
    raw_dir: $('setRawDir').value.trim(),
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
  if (t.id === 'btnRun') {
    (async () => {
      t.disabled = true;
      if (!await ensureServer()) { t.disabled = false; return; }
      await control(state.running ? 'stop' : 'start').catch(() => {});
      t.disabled = false;
    })();
  }
  else if (t.id === 'btnStartServer') ensureServer();
  else if (t.id === 'btnGetToken') {
    (async () => {
      const btn = t;
      btn.disabled = true;
      btn.textContent = 'жду токен… (до 60с)';
      const box = $('bridgeStatus');
      box.classList.remove('hidden');
      box.innerHTML = 'Перезагружаю вкладку chordmini.me. Перехватчик теперь работает постоянно — токен доставится автоматически при первом запросе сайта к Firebase…';
      try {
        await chrome.runtime.sendMessage({ type: 'get-appcheck-token' });
      } catch (e) { /* ignore: reload request failed */ }
      const deadline = Date.now() + 60000;
      let got = false;
      while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 1500));
        try {
          const st = await api('/api/appcheck-token');
          if (st && st.present) {
            got = true;
            break;
          }
        } catch (e) { /* engine down */ }
      }
      btn.disabled = false;
      btn.textContent = 'Получить токен';
      if (got) {
        box.innerHTML = '<span class="ok">Токен получен и доставлен движку ✓ Можно запускать обработку</span>';
        refresh();
      } else {
        box.innerHTML = '<span class="bad">Токен ещё не пойман. Обновите вкладку chordmini.me или подождите — перехватчик активен постоянно, токен доставится сам при первом запросе к Firebase (если движок запущен).</span>';
      }
    })();
  }
  else if (t.id === 'btnOpenOut') api('/api/open-output').catch(() => {});
  else if (t.id === 'btnOpenRaw') api('/api/open-raw').catch(() => {});
  else if (t.id === 'btnOpenSrc') api('/api/open-source').catch(() => {});
  else if (t.id === 'btnClearOut' || t.id === 'btnClearRaw') clearResults(t.id === 'btnClearRaw');
  else if (t.id === 'btnSaveSettings') saveSettings();
  else if (t.id === 'btnClearLog') { $('logBox').innerHTML = ''; lastLogSeq = 0; }
  else if (t.dataset.dl) {
    const a = document.createElement('a');
    a.href = API + '/api/file/' + encodeURIComponent(t.dataset.dl);
    a.download = t.dataset.dl;
    document.body.appendChild(a);
    a.click();
    a.remove();
  } else if (t.dataset.mdl) {
    const a = document.createElement('a');
    a.href = API + '/api/file/' + encodeURIComponent(t.dataset.mdl);
    a.download = t.dataset.mdl;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
});

refresh();
setInterval(refresh, 1000);