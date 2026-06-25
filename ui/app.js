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

function toast(msg, kind) {
  const wrap = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = msg;
  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); }, 4500);
}

// Pull the latest bars from TopStep (both datasets), then refresh the view.
async function fetchLatest() {
  const btn = document.getElementById('btn-fetch');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Fetching…';
  try {
    const res = await (await fetch('/api/fetch', { method: 'POST' })).json();
    if (res.ok) {
      const added = (res.results || []).reduce((a, r) => a + (r.added || 0), 0);
      const parts = (res.results || []).map(r => `${r.symbol} +${fmt(r.added)}`);
      toast(added ? `Added ${fmt(added)} bars (${parts.join(', ')})` : 'Already up to date',
            'success');
    } else {
      const err = (res.results || []).find(r => r.error);
      toast('Fetch failed: ' + (err ? err.error : 'unknown error'), 'error');
    }
  } catch (e) {
    toast('Fetch failed: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
    load();
  }
}

async function load() {
  let s;
  try {
    s = await (await fetch('/api/status')).json();
  } catch (e) {
    document.getElementById('feed-label').textContent = 'API offline';
    return;
  }

  var feed = s.feed || {};
  var stale = !!(feed.market_open && feed.up === false);   // session open but no recent data
  var fdot = document.getElementById('feed-dot');
  fdot.classList.toggle('on', !!s.ok && !stale);
  fdot.classList.toggle('warn', stale);
  var flabel = s.ok ? 'API online' : 'API';
  if (feed.market_open) flabel = (stale ? 'Feed stale' : 'Live feed') + (feed.age != null ? ' ' + feed.age + 's' : '');
  document.getElementById('feed-label').textContent = flabel;
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
