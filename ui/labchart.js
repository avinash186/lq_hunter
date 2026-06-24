/* Labeling workspace: NQ + ES side-by-side, annotation tools, save/continue.
   Isolated from the charts page. Annotations are LABELS; features are auto-computed
   at train time (Phase 2). v1 trade edit = via the list inputs (drag is a follow-up). */
(function () {
  'use strict';
  var LC = window.LightweightCharts;
  var SYMS = ['NQ', 'ES'];
  var ch = {};                                   // sym -> {chart, series, ovl, ctx, bars}
  var WS = { name: '', trades: [], annotations: [] };
  var tool = 'cursor', tf = '5m', inited = false, smtPend = null;
  var FVG_COLORS = { '1m': '#8892a4', '2m': '#7e9cff', '3m': '#26a69a', '5m': '#42a5f5',
                     '15m': '#ab47bc', '1h': '#ffa726', '1d': '#ef5350' };

  function $(id) { return document.getElementById(id); }
  function status(m) { var s = $('lab-ws-status'); if (s) s.textContent = m || ''; }
  function hexA(h, a) {
    h = h.replace('#', ''); if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var n = parseInt(h, 16); return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }
  function lc(sym) { return sym.toLowerCase(); }

  function mkChart(sym) {
    var host = $('lab-chart-' + lc(sym)), ovl = $('lab-ovl-' + lc(sym));
    var chart = LC.createChart(host, {
      width: host.clientWidth || 400, height: host.clientHeight || 360,
      layout: { background: { type: 'solid', color: '#080a12' }, textColor: '#8892a4', fontSize: 11 },
      grid: { vertLines: { color: 'rgba(37,40,64,.35)' }, horzLines: { color: 'rgba(37,40,64,.35)' } },
      rightPriceScale: { borderColor: '#252840' },
      timeScale: { borderColor: '#252840', timeVisible: true, secondsVisible: false },
      crosshair: { mode: LC.CrosshairMode.Normal },
    });
    var series = chart.addCandlestickSeries({ upColor: '#26a69a', downColor: '#ef5350',
      wickUpColor: '#26a69a', wickDownColor: '#ef5350', borderVisible: false });
    var o = { chart: chart, series: series, ovl: ovl, ctx: ovl.getContext('2d'), bars: [] };
    ch[sym] = o;
    chart.timeScale().subscribeVisibleLogicalRangeChange(function () { redraw(sym); });
    ovl.addEventListener('mousedown', function (e) { onDown(sym, e); });
    new ResizeObserver(function () {
      chart.applyOptions({ width: host.clientWidth, height: host.clientHeight }); sizeOvl(sym); redraw(sym);
    }).observe(host);
    sizeOvl(sym);
    return o;
  }
  function sizeOvl(sym) {
    var o = ch[sym], host = $('lab-chart-' + lc(sym));
    var w = host.clientWidth, h = host.clientHeight, dpr = window.devicePixelRatio || 1;
    o.ovl.width = w * dpr; o.ovl.height = h * dpr; o.ovl.style.width = w + 'px'; o.ovl.style.height = h + 'px';
    o.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    o.ovl.style.pointerEvents = tool === 'cursor' ? 'none' : 'auto';
  }
  function loadBars(sym) {
    var o = ch[sym]; if (!o) return Promise.resolve();
    return fetch('/api/bars/' + sym + '?tf=' + tf + '&limit=3000').then(function (r) { return r.json(); }).then(function (d) {
      o.bars = d.bars || [];
      o.series.setData(o.bars.map(function (b) { return { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close }; }));
      o.chart.timeScale().fitContent(); redraw(sym);
    }).catch(function () {});
  }
  function xToTime(sym, x) { return ch[sym].chart.timeScale().coordinateToTime(x); }
  function timeToX(sym, t) { return ch[sym].chart.timeScale().timeToCoordinate(t); }
  function yToPrice(sym, y) { return ch[sym].series.coordinateToPrice(y); }
  function priceToY(sym, p) { return ch[sym].series.priceToCoordinate(p); }
  function snap(sym, t) {
    var b = ch[sym].bars; if (!b.length) return t; var best = b[0].time, bd = 1e18;
    for (var i = 0; i < b.length; i++) { var d = Math.abs(b[i].time - t); if (d < bd) { bd = d; best = b[i].time; } } return best;
  }
  function atrEst(sym) {
    var b = ch[sym].bars; if (b.length < 20) return 10; var s = 0, n = 0;
    for (var i = b.length - 20; i < b.length; i++) { s += b[i].high - b[i].low; n++; } return n ? s / n : 10;
  }

  function onDown(sym, e) {
    if (tool === 'cursor') return;
    var r = ch[sym].ovl.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
    var t = xToTime(sym, px), price = yToPrice(sym, py);
    if (t == null || price == null) return;
    t = snap(sym, t);
    if (tool === 'long' || tool === 'short') {
      var a = atrEst(sym), side = tool === 'long' ? 1 : 0, sgn = side ? 1 : -1;
      WS.trades.push({ sym: sym, side: side, entry_ts: t, entry: price, tp: price + sgn * 1.5 * a, sl: price - sgn * a });
      setTool('cursor'); renderList(); redraw(sym);
    } else if (tool === 'level') {
      WS.annotations.push({ type: 'level', sym: sym, price: price, t: t }); redraw(sym);
    } else if (tool === 'eqhl') {
      WS.annotations.push({ type: 'eqhl', sym: sym, price: price, t: t }); redraw(sym);
    } else if (tool === 'fvg' || tool === 'ifvg') {
      var bb = ch[sym].bars, idx = -1; for (var i = 0; i < bb.length; i++) { if (bb[i].time === t) { idx = i; break; } }
      var top = price + 0.5 * atrEst(sym), bot = price - 0.5 * atrEst(sym);
      if (idx >= 1 && idx < bb.length - 1) { top = Math.max(bb[idx - 1].high, bb[idx + 1].low); bot = Math.min(bb[idx - 1].high, bb[idx + 1].low); }
      WS.annotations.push({ type: tool, sym: sym, tf: tf, top: top, bottom: bot, t: t }); redraw(sym);
    } else if (tool === 'smt') {
      if (!smtPend) { smtPend = { sym: sym, t: t, price: price }; status('SMT: now click the ' + (sym === 'NQ' ? 'ES' : 'NQ') + ' pivot'); }
      else if (smtPend.sym !== sym) { WS.annotations.push({ type: 'smt', a: smtPend, b: { sym: sym, t: t, price: price } }); smtPend = null; status('SMT divergence saved'); SYMS.forEach(redraw); }
      else { smtPend = { sym: sym, t: t, price: price }; }      // re-pick same side
    }
  }

  function redraw(sym) {
    var o = ch[sym]; if (!o) return;
    var ctx = o.ctx, W = o.ovl.clientWidth, H = o.ovl.clientHeight;
    ctx.clearRect(0, 0, W, H);
    WS.annotations.forEach(function (an) {
      if (an.type === 'level' && an.sym === sym) { var y = priceToY(sym, an.price); if (y != null) { line(ctx, 0, y, W, y, 'rgba(255,200,80,.85)', [5, 3]); } }
      else if (an.type === 'eqhl' && an.sym === sym) { var y2 = priceToY(sym, an.price); if (y2 != null) line(ctx, 0, y2, W, y2, 'rgba(126,156,255,.85)', [2, 2]); }
      else if ((an.type === 'fvg' || an.type === 'ifvg') && an.sym === sym) {
        var x = timeToX(sym, an.t), yt = priceToY(sym, an.top), yb = priceToY(sym, an.bottom);
        if (yt != null && yb != null) { var x0 = x == null ? 0 : x, col = FVG_COLORS[an.tf] || '#42a5f5';
          ctx.fillStyle = hexA(col, an.type === 'ifvg' ? 0.13 : 0.22); ctx.fillRect(x0, Math.min(yt, yb), W - x0, Math.abs(yt - yb));
          ctx.strokeStyle = hexA(col, 0.6); ctx.setLineDash([]); ctx.strokeRect(x0, Math.min(yt, yb), W - x0, Math.abs(yt - yb));
          ctx.fillStyle = hexA(col, 0.95); ctx.font = '9px system-ui'; ctx.fillText((an.type === 'ifvg' ? 'IFVG ' : 'FVG ') + an.tf, x0 + 3, Math.min(yt, yb) + 10); }
      } else if (an.type === 'smt') {
        [an.a, an.b].forEach(function (pt) { if (pt && pt.sym === sym) { var x = timeToX(sym, pt.t), y = priceToY(sym, pt.price); if (x != null && y != null) { ctx.fillStyle = '#00d2ff'; ctx.beginPath(); ctx.arc(x, y, 4, 0, 6.3); ctx.fill(); } } });
      }
    });
    WS.trades.forEach(function (tr) {
      if (tr.sym !== sym) return;
      var x1 = timeToX(sym, tr.entry_ts); if (x1 == null) return;
      var x2 = Math.min(W, x1 + 90), ye = priceToY(sym, tr.entry), yt = priceToY(sym, tr.tp), ys = priceToY(sym, tr.sl);
      if (ye == null || yt == null || ys == null) return;
      ctx.fillStyle = 'rgba(38,166,154,.26)'; ctx.fillRect(x1, Math.min(ye, yt), x2 - x1, Math.abs(yt - ye));
      ctx.fillStyle = 'rgba(239,83,80,.22)'; ctx.fillRect(x1, Math.min(ye, ys), x2 - x1, Math.abs(ys - ye));
      ctx.setLineDash([]); ctx.strokeStyle = '#cbd5e1'; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x1, ye); ctx.lineTo(x2, ye); ctx.stroke();
    });
  }
  function line(ctx, x1, y1, x2, y2, color, dash) { ctx.setLineDash(dash || []); ctx.lineWidth = 1; ctx.strokeStyle = color; ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); ctx.setLineDash([]); }

  function setTool(t) {
    tool = t;
    document.querySelectorAll('.lab-tool').forEach(function (b) { b.classList.toggle('active', b.getAttribute('data-tool') === t); });
    SYMS.forEach(function (s) { if (ch[s]) ch[s].ovl.style.pointerEvents = t === 'cursor' ? 'none' : 'auto'; });
    if (t !== 'smt') smtPend = null;
  }
  function renderList() {
    $('lab-trade-count').textContent = WS.trades.length;
    var el = $('lab-trade-list');
    el.innerHTML = WS.trades.length ? WS.trades.map(function (tr, i) {
      return '<div class="lab-trow"><span class="lab-tside ' + (tr.side ? 'long' : 'short') + '">' + (tr.side ? 'LONG' : 'SHORT') + ' ' + tr.sym + '</span>' +
        'E<input data-i="' + i + '" data-k="entry" value="' + tr.entry.toFixed(2) + '">' +
        'TP<input data-i="' + i + '" data-k="tp" value="' + tr.tp.toFixed(2) + '">' +
        'SL<input data-i="' + i + '" data-k="sl" value="' + tr.sl.toFixed(2) + '">' +
        '<button class="lab-del" data-del="' + i + '">&times;</button></div>';
    }).join('') : '<div class="empty">No trades yet — pick Long/Short and click a chart.</div>';
    el.querySelectorAll('input').forEach(function (inp) {
      inp.onchange = function () { var i = +inp.getAttribute('data-i'), k = inp.getAttribute('data-k'), v = parseFloat(inp.value); if (!isNaN(v)) { WS.trades[i][k] = v; redraw(WS.trades[i].sym); } };
    });
    el.querySelectorAll('button[data-del]').forEach(function (b) {
      b.onclick = function () { var t = WS.trades[+b.getAttribute('data-del')]; WS.trades.splice(+b.getAttribute('data-del'), 1); renderList(); if (t) redraw(t.sym); };
    });
  }
  function refreshList() {
    fetch('/api/lab/workspaces').then(function (r) { return r.json(); }).then(function (d) {
      $('lab-ws-list').innerHTML = '<option value="">— open workspace —</option>' + (d.workspaces || []).map(function (w) { return '<option>' + w.name + '</option>'; }).join('');
    }).catch(function () {});
  }
  function saveWs() {
    var name = $('lab-ws-name').value.trim(); if (!name) { status('name required'); return; }
    WS.name = name; WS.updated = Date.now();
    fetch('/api/lab/workspace/' + encodeURIComponent(name), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(WS) })
      .then(function (r) { return r.json(); }).then(function (res) { status(res.error ? res.error : 'saved "' + name + '"'); if (!res.error) refreshList(); })
      .catch(function () { status('save failed'); });
  }
  function newWs() { WS = { name: '', trades: [], annotations: [] }; $('lab-ws-name').value = ''; smtPend = null; renderList(); SYMS.forEach(redraw); status('new workspace'); }
  function openWs(name) {
    if (!name) return;
    fetch('/api/lab/workspace/' + encodeURIComponent(name)).then(function (r) { return r.json(); }).then(function (d) {
      WS = { name: d.name || name, trades: d.trades || [], annotations: d.annotations || [] };
      $('lab-ws-name').value = WS.name; renderList(); SYMS.forEach(redraw); status('opened "' + name + '"');
    }).catch(function () { status('open failed'); });
  }
  function init() {
    if (inited) return; inited = true;
    SYMS.forEach(function (s) { mkChart(s); loadBars(s); });
    document.querySelectorAll('.lab-tool').forEach(function (b) { b.onclick = function () { setTool(b.getAttribute('data-tool')); }; });
    $('lab-tf').onchange = function () { tf = this.value; SYMS.forEach(loadBars); };
    $('lab-ws-save').onclick = saveWs; $('lab-ws-new').onclick = newWs;
    $('lab-ws-list').onchange = function () { openWs(this.value); };
    refreshList(); renderList();
  }
  window.labchart = {
    show: function () {
      init();
      setTimeout(function () {
        SYMS.forEach(function (s) {
          if (!ch[s]) return; var host = $('lab-chart-' + lc(s));
          ch[s].chart.applyOptions({ width: host.clientWidth, height: host.clientHeight }); sizeOvl(s); redraw(s);
        });
      }, 60);
    },
  };
})();
