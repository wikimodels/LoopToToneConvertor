const el = document.getElementById('status');

fetch('http://127.0.0.1:8002/api/appcheck-token', { method: 'GET' })
  .then((r) => r.json())
  .then(() => {
    el.textContent = 'движок доступен';
    el.className = 'ok';
  })
  .catch(() => {
    el.textContent = 'движок не запущен';
    el.className = 'bad';
  });