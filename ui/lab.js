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

  var lab = {
    open: function () { lab.loadModels(); },
    tab: function (b) {
      document.querySelectorAll('.lab-tab').forEach(function (t) { t.classList.remove('active'); });
      b.classList.add('active');
      var p = b.getAttribute('data-lab');
      document.querySelectorAll('.lab-pane').forEach(function (x) { x.classList.remove('active'); });
      el('lab-' + p).classList.add('active');
      if (p === 'labeling' && window.labchart) window.labchart.show();
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
})();
