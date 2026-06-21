/* Minimal UI logic: nav, clock, and /api/status rendering. */

function nav(el) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  el.classList.add('active');
  const page = el.dataset.page;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + page).classList.add('active');
  document.getElementById('tb-title').textContent = el.textContent.trim();
  if (window.innerWidth <= 920) sbToggle(true);
}

function sbToggle(forceClose) {
  document.body.classList.toggle('sb-open', forceClose ? false : undefined);
}

function clock() {
  const d = new Date();
  document.getElementById('clock').textContent = d.toLocaleTimeString();
}
setInterval(clock, 1000); clock();

function fmt(n) { return n == null ? '—' : Number(n).toLocaleString(); }

async function load() {
  let s;
  try {
    s = await (await fetch('/api/status')).json();
  } catch (e) {
    document.getElementById('feed-label').textContent = 'API offline';
    return;
  }

  document.getElementById('feed-dot').classList.toggle('on', !!s.ok);
  document.getElementById('feed-label').textContent = s.ok ? 'API online' : 'API';
  document.getElementById('ts-dot').classList.toggle('on', !!s.topstep_configured);
  document.getElementById('tg-dot').classList.toggle('on', !!s.telegram_configured);

  const ds = (s.datasets || []).filter(d => d.exists);
  document.getElementById('stat-datasets').textContent = ds.length;
  document.getElementById('stat-bars').textContent = fmt(ds.reduce((a, d) => a + (d.rows || 0), 0));
  document.getElementById('stat-models').textContent = (s.models || []).length;
  const aucs = (s.models || []).map(m => m.holdout_auc).filter(x => x != null);
  document.getElementById('stat-auc').textContent = aucs.length ? Math.max(...aucs).toFixed(3) : '—';

  document.getElementById('datasets-body').innerHTML = ds.length ? ds.map(d => `
    <tr><td class="td-name">${d.symbol}</td><td>${fmt(d.rows)}</td>
    <td>${(d.start || '').slice(0, 10)}</td><td>${(d.end || '').slice(0, 10)}</td>
    <td>${d.mb} MB</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">No datasets found.</td></tr>';

  const m = s.models || [];
  document.getElementById('models-body').innerHTML = m.length ? m.map(x => `
    <tr><td class="td-name">${x.symbol}_${x.timeframe}_${x.created_utc}</td>
    <td><span class="tag tag-accent">${x.symbol}</span></td><td>${x.timeframe}</td>
    <td>${fmt(x.rows)}</td><td class="${x.holdout_auc >= 0.5 ? 'td-pos' : 'td-neg'}">${x.holdout_auc}</td>
    <td>${x.created_utc}</td></tr>`).join('')
    : '<tr><td colspan="6" class="empty">No models yet — run the trainer.</td></tr>';
}
load();
setInterval(load, 10000);
