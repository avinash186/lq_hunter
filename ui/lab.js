/* Model Lab: Models tab (compare table + set active) and Labeling tab (next build).
   Self-contained — does not touch the charts page. */
(function () {
  'use strict';
  function el(id) { return document.getElementById(id); }
  function f2(v) { return v == null || v !== v ? '—' : (+v).toFixed(2); }
  function pct(v) { return v == null || v !== v ? '—' : (v * 100).toFixed(1) + '%'; }
  function pair(a, b, fmt) { return fmt(a) + '  /  ' + fmt(b); }

  function row(m) {
    var mt = m.metrics || {}, is = mt.is || {}, oos = mt.oos || {}, sh = m.shadow || {};
    var dsr = oos.dsr, gap = mt.is_oos_gap;
    var overfit = (gap != null && gap > 0.10) || (dsr != null && dsr < 0.90);
    // LIVE = the one model that trades and gets retrained. Others get a "Set live" button.
    var act = m.live
      ? '<span class="lab-badge">LIVE</span>'
      : '<button class="btn btn-secondary btn-sm" data-act="' + m.name + '">Set live</button>';
    var usd = oos.total_usd != null ? '$' + Math.round(oos.total_usd).toLocaleString() : '—';
    var shadow = sh.trades != null
      ? sh.trades + '/100' + (sh.pf != null ? ' · PF ' + f2(sh.pf) : '')
      : '—';
    return '<tr' + (m.live ? ' class="lab-active-row"' : '') + '>' +
      '<td><strong>' + m.name + '</strong></td>' +
      '<td>' + (m.status || 'trained') + '</td>' +
      '<td>' + (m.parent || '—') + '</td>' +
      '<td>' + shadow + '</td>' +
      '<td>' + pair(is.win, oos.win, pct) + '</td>' +
      '<td>' + pair(is.pf, oos.pf, f2) + '</td>' +
      '<td>' + pair(is.sharpe, oos.sharpe, f2) + '</td>' +
      '<td>' + pair(is.maxdd_R, oos.maxdd_R, f2) + '</td>' +
      '<td class="' + (overfit ? 'c-red' : 'c-green') + '">' + f2(dsr) + '</td>' +
      '<td class="' + (overfit ? 'c-red' : '') + '">' + (gap != null ? (gap * 100).toFixed(1) + 'pp' : '—') + '</td>' +
      '<td>' + usd + '</td>' +
      '<td>' + act + '</td></tr>';
  }

  // ── Metrics sub-tab: per-model forward (live+shadow) + backtest-OOS, over a chosen window ──
  function usd(v) { return v == null || v !== v ? '—' : '$' + Math.round(v).toLocaleString(); }
  function metricsRow(m) {
    var fwd = m.fwd || {}, oos = m.oos || {}, is = m.is || {};
    return '<tr' + (m.live ? ' class="lab-active-row"' : '') + '>' +
      '<td><strong>' + m.name + '</strong>' + (m.live ? ' <span class="lab-badge">LIVE</span>' : '') + '</td>' +
      '<td>' + (m.status || '—') + '</td>' +
      '<td>' + (fwd.trades || 0) + '</td>' +
      '<td>' + pct(fwd.win) + '</td>' +
      '<td>' + f2(fwd.pf) + '</td>' +
      '<td>' + usd(fwd.total_usd) + '</td>' +
      '<td>' + (oos.trades || is.trades || 0) + '</td>' +
      '<td>' + pct(oos.win != null ? oos.win : is.win) + '</td>' +
      '<td>' + f2(oos.pf != null ? oos.pf : is.pf) + '</td>' +
      '<td>' + f2(oos.sharpe != null ? oos.sharpe : is.sharpe) + '</td>' +
      '<td>' + usd(oos.total_usd != null ? oos.total_usd : is.total_usd) + '</td></tr>';
  }

  var lab = {
    open: function () { lab.loadModels(); },
    tab: function (b) {
      document.querySelectorAll('.lab-tab').forEach(function (t) { t.classList.remove('active'); });
      b.classList.add('active');
      var p = b.getAttribute('data-lab');
      document.querySelectorAll('.lab-pane').forEach(function (x) { x.classList.remove('active'); });
      el('lab-' + p).classList.add('active');
      if (p === 'labeling' && window.labchart) window.labchart.show();
      if (p === 'metrics') lab.loadMetrics();
    },
    loadMetrics: function () {
      var body = el('lab-metrics-body'); if (!body) return;
      var tf = (el('metrics-timeframe') || {}).value || '';
      var scope = (el('metrics-scope') || {}).value || 'all';
      fetch('/api/models/metrics?timeframe=' + encodeURIComponent(tf) + '&scope=' + encodeURIComponent(scope))
        .then(function (r) { return r.json(); }).then(function (d) {
          var ms = (d && d.models) || [];
          body.innerHTML = ms.length ? ms.map(metricsRow).join('')
            : '<tr><td colspan="11" class="empty">No models.</td></tr>';
        }).catch(function () {
          body.innerHTML = '<tr><td colspan="11" class="empty">Failed to load metrics.</td></tr>';
        });
    },
    loadModels: function () {
      var body = el('lab-models-body'); if (!body) return;
      fetch('/api/models').then(function (r) { return r.json(); }).then(function (d) {
        var ms = (d && d.models) || [];
        body.innerHTML = ms.length
          ? ms.map(row).join('')
          : '<tr><td colspan="12" class="empty">No models yet — train one in the Labeling tab.</td></tr>';
        body.querySelectorAll('button[data-act]').forEach(function (btn) {
          btn.onclick = function () { lab.activate(btn.getAttribute('data-act')); };
        });
      }).catch(function () {
        body.innerHTML = '<tr><td colspan="12" class="empty">Failed to load models.</td></tr>';
      });
    },
    activate: function (name) {
      fetch('/api/models/' + encodeURIComponent(name) + '/activate', { method: 'POST' })
        .then(function (r) { return r.json(); }).then(function () { lab.loadModels(); })
        .catch(function () {});
    },
  };
  window.lab = lab;

  // Controls: re-query metrics when the window/scope changes.
  document.addEventListener('change', function (e) {
    if (e.target && (e.target.id === 'metrics-timeframe' || e.target.id === 'metrics-scope')) lab.loadMetrics();
  });
  // Live refresh: when ANY model (live or shadow) closes a trade, refresh the metrics table if visible.
  (function () {
    try {
      var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      var ws = new WebSocket(proto + '//' + location.host + '/ws');
      ws.onmessage = function (msg) {
        var ev; try { ev = JSON.parse(msg.data); } catch (x) { return; }
        if ((ev.e === 'trade_close' || ev.e === 'shadow_close') &&
            el('lab-metrics') && el('lab-metrics').classList.contains('active')) lab.loadMetrics();
      };
    } catch (x) {}
  })();
})();
