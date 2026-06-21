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
  document.getElementById('clock').textContent = new Date().toLocaleTimeString();
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
  document.getElementById('stat-disk').textContent =
    ds.length ? ds.reduce((a, d) => a + (d.mb || 0), 0).toFixed(0) + ' MB' : '—';

  // Coverage = widest span across datasets, in years.
  const starts = ds.map(d => d.start).filter(Boolean).sort();
  const ends = ds.map(d => d.end).filter(Boolean).sort();
  if (starts.length && ends.length) {
    const yrs = (new Date(ends[ends.length - 1]) - new Date(starts[0])) / (365.25 * 864e5);
    document.getElementById('stat-span').textContent = yrs.toFixed(1) + 'y';
    document.getElementById('stat-span-sub').textContent =
      starts[0].slice(0, 4) + ' → ' + ends[ends.length - 1].slice(0, 4);
  }

  document.getElementById('datasets-body').innerHTML = ds.length ? ds.map(d => `
    <tr><td class="td-name">${d.symbol}</td><td>${fmt(d.rows)}</td>
    <td>${(d.start || '').slice(0, 10)}</td><td>${(d.end || '').slice(0, 10)}</td>
    <td>${d.mb} MB</td></tr>`).join('')
    : '<tr><td colspan="5" class="empty">No datasets found.</td></tr>';
}
load();
setInterval(load, 10000);
