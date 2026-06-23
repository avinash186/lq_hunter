/* TradingView-style candlestick chart (Lightweight Charts).
   Candles + volume for NQ/ES, multi-timeframe, lazy back-paging, themeable,
   with drawing tools (horizontal + trend lines) and persisted zoom. */
(function () {
  'use strict';

  var LIMIT = 20000;          // bars per request (server caps to what's available)
  var PAGE = 10000;           // older-bars chunk when scrolling left
  // ── persisted settings ──
  var S = Object.assign({
    sym: 'NQ', tf: '1m',
    up: '#26a69a', down: '#ef5350',
    volOn: true, volOpacity: 55, theme: 'dark', grid: true,
  }, JSON.parse(localStorage.getItem('cx') || '{}'));
  function save() { localStorage.setItem('cx', JSON.stringify(S)); scheduleServerSave(); }

  // Settings persist server-side (survives the changing cloudflared URL).
  var srvSaveT = 0;
  function scheduleServerSave() { clearTimeout(srvSaveT); srvSaveT = setTimeout(saveServer, 1500); }
  function saveServer() {
    try {
      fetch('/api/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cx: S, cxlevels: LV, cxscore: SCORE }),
      }).catch(function () {});
    } catch (e) {}
  }
  function mergeServer(srv) {
    if (!srv) return;
    if (srv.cx) S = Object.assign(S, srv.cx);
    var c = srv.cxlevels;
    if (c) {
      if (typeof c.unsweptOnly === 'boolean') LV.unsweptOnly = c.unsweptOnly;
      if (c.mode) LV.mode = c.mode;
      if (typeof c.closestN === 'number') LV.closestN = c.closestN;
      if (typeof c.legend === 'boolean') LV.legend = c.legend;
      if (typeof c.midOn === 'boolean') LV.midOn = c.midOn;
      if (typeof c._v === 'number') LV._v = c._v;
      if (c.types) LVDEF.forEach(function (t) { if (c.types[t.key]) LV.types[t.key] = Object.assign(LV.types[t.key], c.types[t.key]); });
    }
    var sc = srv.cxscore;
    if (sc) {
      if (typeof sc.on === 'boolean') SCORE.on = sc.on;
      if (sc.band) SCORE.band = sc.band;
      if (sc.legendOp != null) SCORE.legendOp = sc.legendOp;
      if (sc.view && typeof sc.view.lo === 'number') { scoreView.lo = sc.view.lo; scoreView.hi = sc.view.hi; }
      if (sc.lines) ['mid', 'up', 'down'].forEach(function (k) { if (sc.lines[k]) Object.assign(SCORE.lines[k], sc.lines[k]); });
    }
  }

  // ── liquidity-level settings (per type) ──
  var LVDEF = [
    { key: 'week',     kind: 'week',     tier: 1, group: 'Tier 1 · Weekly',         name: 'Prior Week H/L',
      def: { on: true, color: '#f0b90b', width: 2, style: 'solid', opacity: 100, label: true,  ext: null } },
    { key: 'day',      kind: 'day',      tier: 2, group: 'Tier 2 · Daily',          name: 'Prior Day H/L',
      def: { on: true, color: '#42a5f5', width: 2, style: 'solid', opacity: 100, label: true,  ext: 14 } },
    { key: 'ib',       kind: 'ib',       tier: 3, group: 'Tier 3 · Initial Balance', name: 'Initial Balance (9:30–10:30)',
      def: { on: true, color: '#ec407a', width: 1, style: 'solid', opacity: 90, label: true, ext: 5, mid: true, midColor: '#f48fb1', box: false, boxColor: '#ec407a', boxOpacity: 7 } },
    { key: 'kz_asia',  kind: 'killzone', session: 'asia',   tier: 3, group: 'Tier 3 · Killzones', name: 'Asia (20–00)',
      def: { on: true, color: '#ab47bc', width: 1, style: 'solid', opacity: 85, label: true, ext: 5, box: true, boxColor: '#ab47bc', boxOpacity: 8 } },
    { key: 'kz_london', kind: 'killzone', session: 'london', tier: 3, group: 'Tier 3 · Killzones', name: 'London (02–05)',
      def: { on: true, color: '#26c6da', width: 1, style: 'solid', opacity: 85, label: true, ext: 5, box: true, boxColor: '#26c6da', boxOpacity: 8 } },
    { key: 'kz_nyam',  kind: 'killzone', session: 'nyam',  tier: 3, group: 'Tier 3 · Killzones', name: 'NY AM (08:30–11)',
      def: { on: true, color: '#66bb6a', width: 1, style: 'solid', opacity: 85, label: true, ext: 5, box: true, boxColor: '#66bb6a', boxOpacity: 8 } },
    { key: 'kz_nypm',  kind: 'killzone', session: 'nypm',  tier: 3, group: 'Tier 3 · Killzones', name: 'NY PM (13:30–16)',
      def: { on: true, color: '#ffa726', width: 1, style: 'solid', opacity: 85, label: true, ext: 5, box: true, boxColor: '#ffa726', boxOpacity: 8 } },
    { key: 'macro',    kind: 'macro',    tier: 4, group: 'Tier 4 · Macros',         name: 'Macros (:50–:10)',
      def: { on: true, color: '#b0bec5', width: 1, style: 'solid', opacity: 75, label: true, ext: 5, mid: true, midColor: '#78909c', primaryColor: '#00d2ff' } },
    { key: 'between',  kind: 'between',  tier: 5, group: 'Tier 5 · Between Macros',  name: 'Between (:10–:50)',
      def: { on: true, color: '#607d8b', width: 1, style: 'solid', opacity: 45, label: true, ext: 5, mid: true, midColor: '#546e7a' } },
  ];
  var LV = (function () {
    var saved = JSON.parse(localStorage.getItem('cxlevels') || 'null') || {};
    var s = { unsweptOnly: saved.unsweptOnly !== false, mode: saved.mode || 'all',
              closestN: saved.closestN || 3, legend: saved.legend !== false,
              midOn: !!saved.midOn, _v: saved._v || 0, types: {} };
    LVDEF.forEach(function (t) { s.types[t.key] = Object.assign({}, t.def, (saved.types || {})[t.key] || {}); });
    return s;
  })();
  function saveLV() { localStorage.setItem('cxlevels', JSON.stringify(LV)); scheduleServerSave(); }
  function lvDef(key) { return LVDEF.find(function (t) { return t.key === key; }); }

  var levelData = { levels: [], zones: [] };
  var lvFetchT = 0, lvReq = 0;

  var ABBR = {
    week_high: 'PWH', week_low: 'PWL', day_high: 'PDH', day_low: 'PDL',
    kz_asia_high: 'AsiaH', kz_asia_low: 'AsiaL', kz_london_high: 'LonH', kz_london_low: 'LonL',
    kz_nyam_high: 'AMh', kz_nyam_low: 'AMl', kz_nypm_high: 'PMh', kz_nypm_low: 'PMl',
    macro_high: 'McH', macro_low: 'McL', between_high: 'BtH', between_low: 'BtL',
    ib_high: 'IBH', ib_low: 'IBL',
  };

  var chart, candle, volume;
  var bars = [];              // [{time,open,high,low,close,volume}] ascending
  var hasMore = false, loading = false, reqId = 0, saveT = 0;

  var $ = function (id) { return document.getElementById(id); };

  function hexToRgba(hex, a) {
    var n = parseInt(hex.slice(1), 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  // ───────────────────────── init ─────────────────────────
  // Times from the server are already ET wall-clock ("ET-naive" epoch), so we
  // format them as UTC to render the Eastern clock with ET day boundaries.
  var ET = 'UTC';
  function etTime(ts) { return new Date(ts * 1000).toLocaleTimeString('en-US', { timeZone: ET, hour: '2-digit', minute: '2-digit', hour12: false }); }
  function etDate(ts) { return new Date(ts * 1000).toLocaleDateString('en-US', { timeZone: ET, month: 'short', day: 'numeric' }); }
  function etFull(ts) { return new Date(ts * 1000).toLocaleString('en-US', { timeZone: ET, month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }) + ' ET'; }

  function applyTheme() {
    var light = S.theme === 'light';
    document.body.classList.toggle('theme-light', light);
    var btn = $('theme-toggle'); if (btn) btn.textContent = light ? '☀' : '☽';
    if (chart) {
      chart.applyOptions({
        layout: { background: { type: 'solid', color: light ? '#ffffff' : '#080a12' },
                  textColor: light ? '#18202e' : '#8892a4' },
        grid: { vertLines: { color: light ? 'rgba(20,30,50,.07)' : 'rgba(37,40,64,.35)', visible: S.grid !== false },
                horzLines: { color: light ? 'rgba(20,30,50,.07)' : 'rgba(37,40,64,.35)', visible: S.grid !== false } },
        rightPriceScale: { borderColor: light ? '#dde3ec' : '#252840' },
        timeScale: { borderColor: light ? '#dde3ec' : '#252840' },
      });
      redrawLevels();
      if (midLine) midLine.applyOptions({ color: midColor() });
    }
  }

  function init() {
    var el = $('cx-chart');
    if (!el || !window.LightweightCharts) return;
    var TT = LightweightCharts.TickMarkType;
    document.body.classList.toggle('theme-light', S.theme === 'light');   // before paint

    chart = LightweightCharts.createChart(el, {
      autoSize: true,
      layout: { background: { type: 'solid', color: '#080a12' }, textColor: '#8892a4', fontSize: 11,
                fontFamily: "-apple-system,Segoe UI,system-ui,sans-serif" },
      grid: { vertLines: { color: 'rgba(37,40,64,.35)' }, horzLines: { color: 'rgba(37,40,64,.35)' } },
      rightPriceScale: { borderColor: '#252840' },
      localization: { timeFormatter: etFull },          // crosshair time label → ET
      timeScale: {
        borderColor: '#252840', timeVisible: true, secondsVisible: false, rightOffset: 6,
        tickMarkFormatter: function (time, tickType) {  // axis labels → ET
          return (tickType === TT.Time || tickType === TT.TimeWithSeconds) ? etTime(time) : etDate(time);
        },
      },
      crosshair: {
        mode: LightweightCharts.CrosshairMode.Normal,
        vertLine: { labelBackgroundColor: '#00d2ff' },
        horzLine: { labelBackgroundColor: '#00d2ff' },   // price readout on right scale
      },
    });

    candle = chart.addCandlestickSeries({ lastValueVisible: true, priceLineVisible: true });
    volume = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol' });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    applyTheme();
    applyScoreLayout();

    chart.timeScale().subscribeVisibleLogicalRangeChange(function (r) {
      if (r && !loading && hasMore && r.from < 12) loadOlder();
      redrawLevels();
      tickCountdown();
      scheduleLvFetch();
      scheduleScoreFetch();
      scheduleRangeSave();
    });
    wireScore();
    chart.subscribeCrosshairMove(function (param) {
      var b;
      if (param && param.time) {
        var c = param.seriesData.get(candle), v = param.seriesData.get(volume);
        if (c) b = { open: c.open, high: c.high, low: c.low, close: c.close,
                     volume: v ? v.value : null };
      }
      if (!b) b = bars[bars.length - 1];
      if (b) renderLegend(b, param && param.time);
    });

    setupOverlayCanvas();
    wireControls();
    wireLevels();

    // Load server-side settings first, then build/apply everything.
    fetch('/api/settings')
      .then(function (r) { return r.json(); })
      .then(function (srv) { mergeServer(srv); })
      .catch(function () {})
      .then(function () {
        // one-time migration: turn labels on for every level type
        if ((LV._v || 0) < 2) {
          LVDEF.forEach(function (t) { LV.types[t.key].label = true; });
          LV._v = 2; saveLV();
        }
        applyTheme();                 // server may have a different theme
        applyColors();
        applyVolVisible();
        buildLevelsPanel();
        buildLabelLegend();
        syncControls();
        load();
        connectLive();
        setInterval(tickCountdown, 1000);               // bar countdown
        setInterval(saveServer, 60000);                 // autosave every minute
        window.addEventListener('beforeunload', function () {
          try {
            navigator.sendBeacon('/api/settings',
              new Blob([JSON.stringify({ cx: S, cxlevels: LV })], { type: 'application/json' }));
          } catch (e) {}
        });
      });
  }

  // ───────────────────────── data ─────────────────────────
  function fetchBars(before) {
    var u = new URL('/api/bars/' + S.sym, location.origin);
    u.searchParams.set('tf', S.tf);
    u.searchParams.set('limit', before ? PAGE : LIMIT);
    if (before) u.searchParams.set('before', before);
    return fetch(u).then(function (r) { return r.json(); });
  }

  function volColor(b) { return hexToRgba(b.close >= b.open ? S.up : S.down, S.volOpacity / 100); }
  function toVol(arr) { return arr.map(function (b) { return { time: b.time, value: b.volume, color: volColor(b) }; }); }
  function setAll() { candle.setData(bars); volume.setData(toVol(bars)); }

  function rangeKey() { return 'cxrange_' + S.sym + '_' + S.tf; }
  function scheduleRangeSave() {
    clearTimeout(saveT);
    saveT = setTimeout(function () {
      var vr = chart.timeScale().getVisibleRange();
      if (vr) localStorage.setItem(rangeKey(), JSON.stringify(vr));
    }, 400);
  }

  function load() {                 // (re)load newest bars for current sym/tf
    var my = ++reqId;
    setLoading(true);
    clearMid();                     // reset the frozen midpoint on symbol/tf change
    fetchBars().then(function (res) {
      if (my !== reqId) return;
      bars = res.bars || [];
      hasMore = !!res.has_more;
      setAll();
      var saved = JSON.parse(localStorage.getItem(rangeKey()) || 'null');
      if (saved) { try { chart.timeScale().setVisibleRange(saved); } catch (e) { chart.timeScale().scrollToRealTime(); } }
      else chart.timeScale().scrollToRealTime();
      if (bars.length) renderLegend(bars[bars.length - 1]);
      scheduleLvFetch();
      applyScoreLayout();
      scheduleScoreFetch();
      loadFired(S.sym);
      updateMidpoint();
      setLoading(false);
    }).catch(function () { if (my === reqId) setLoading(false); });
  }

  function loadOlder() {
    if (!bars.length) return;
    var my = reqId;
    loading = true; setLoading(true);
    fetchBars(bars[0].time).then(function (res) {
      if (my !== reqId) { loading = false; return; }
      var seen = {}; bars.forEach(function (b) { seen[b.time] = 1; });
      var add = (res.bars || []).filter(function (b) { return !seen[b.time]; });
      if (add.length) {
        var range = chart.timeScale().getVisibleLogicalRange();
        bars = add.concat(bars);
        setAll();
        if (range) chart.timeScale().setVisibleLogicalRange({ from: range.from + add.length, to: range.to + add.length });
        redrawLevels();
      }
      hasMore = !!res.has_more && add.length > 0;
      loading = false; setLoading(false);
    }).catch(function () { loading = false; setLoading(false); });
  }

  // ─────────────────────── appearance ───────────────────────
  function applyColors() {
    candle.applyOptions({
      upColor: S.up, downColor: S.down, borderUpColor: S.up, borderDownColor: S.down,
      wickUpColor: S.up, wickDownColor: S.down,
    });
    if (bars.length) volume.setData(toVol(bars));
  }
  function applyVolVisible() { volume.applyOptions({ visible: S.volOn }); }

  function renderLegend(b, t) {
    var el = $('cx-legend'); if (!el) return;
    var col = b.close >= b.open ? S.up : S.down;
    var f = function (v) { return (+v).toLocaleString(undefined, { maximumFractionDigits: 2 }); };
    var sig = '';
    if (SCORE.on && S.tf === '1m' && scoreData.length) {
      var tt = (t != null) ? t : b.time, sg = null;
      for (var i = scoreData.length - 1; i >= 0; i--) { if (scoreData[i].time <= tt) { sg = scoreData[i]; break; } }
      if (sg && Math.abs(sg.time - tt) <= 60) {
        var sc = sg.side === 1 ? S.up : S.down;
        sig = '<span class="cx-leg-tf">p(up) <b style="color:' + sc + '">' + sg.p_up.toFixed(3) + '</b>' +
              (sg.fire ? (sg.side === 1 ? ' ▲ LONG' : ' ▼ SHORT') : '') + '</span>';
      }
    }
    el.innerHTML =
      '<span class="cx-leg-sym">' + S.sym + '</span><span class="cx-leg-tf">' + S.tf + '</span>' +
      '<span style="color:' + col + '">O <b>' + f(b.open) + '</b>  H <b>' + f(b.high) +
      '</b>  L <b>' + f(b.low) + '</b>  C <b>' + f(b.close) + '</b></span>' +
      '<span class="cx-leg-tf">Vol <b>' + f(b.volume) + '</b></span>' + sig;
  }
  function setLoading(on) { var el = $('cx-loading'); if (el) el.classList.toggle('on', on); }

  // ───────────────── overlay canvas sizing + coordinate helpers ─────────────────
  function setupOverlayCanvas() {
    new ResizeObserver(sizeCanvas).observe($('cx-host'));
    sizeCanvas();
  }
  function sizeCanvas() {
    var c = $('cx-overlay'); if (!c) return;
    var host = $('cx-host'), dpr = window.devicePixelRatio || 1;
    var w = host.clientWidth, h = host.clientHeight;
    c.width = w * dpr; c.height = h * dpr; c.style.width = w + 'px'; c.style.height = h + 'px';
    c.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
    redrawLevels();
  }

  function indexOfTime(t) {           // nearest bar index by time (binary search)
    if (!bars.length) return -1;
    var lo = 0, hi = bars.length - 1;
    while (lo < hi) { var m = (lo + hi) >> 1; if (bars[m].time < t) lo = m + 1; else hi = m; }
    return lo;
  }
  function timeToX(t) {               // works off-screen via logical coordinate
    var i = indexOfTime(t); if (i < 0) return null;
    return chart.timeScale().logicalToCoordinate(i);
  }
  function priceToY(p) { return candle.priceToCoordinate(p); }

  // ── bar countdown: seconds to next candle, pinned at the live price (right) ──
  var TFSEC = { '1m': 60, '2m': 120, '3m': 180, '5m': 300, '15m': 900, '1h': 3600, '1d': 86400, '1w': 604800 };
  function etNaiveSeconds() {            // current ET wall-clock as epoch secs (matches bar times)
    var ps = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
      .formatToParts(new Date());
    var o = {}; ps.forEach(function (x) { o[x.type] = x.value; });
    return Date.UTC(+o.year, +o.month - 1, +o.day, o.hour === '24' ? 0 : +o.hour, +o.minute, +o.second) / 1000;
  }
  function fmtDur(s) {
    s = Math.max(0, Math.floor(s));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), c = s % 60;
    var p = function (n) { return (n < 10 ? '0' : '') + n; };
    return h > 0 ? h + ':' + p(m) + ':' + p(c) : p(m) + ':' + p(c);
  }
  function tickCountdown() {
    var el = $('cx-countdown'); if (!el) return;
    if (!bars.length || !candle) { el.style.display = 'none'; return; }
    var last = bars[bars.length - 1];
    var secs = TFSEC[S.tf] || 60;
    var y = priceToY(last.close);
    if (y == null) { el.style.display = 'none'; return; }
    el.textContent = fmtDur((last.time + secs) - etNaiveSeconds());
    el.style.background = last.close >= last.open ? S.up : S.down;  // match price tag (bar color)
    el.style.display = 'block';                     // explicit (CSS default is none)
    el.style.top = Math.round(y + 17) + 'px';      // just below the live-price tag
  }

  // ─────────────────────── controls ───────────────────────
  function setActive(sel, attr, val) {
    document.querySelectorAll(sel).forEach(function (b) { b.classList.toggle('active', b.getAttribute(attr) === val); });
  }
  function syncControls() {
    setActive('.cx-sym', 'data-sym', S.sym);
    setActive('.cx-tf', 'data-tf', S.tf);
    $('cx-vol-toggle').classList.toggle('active', S.volOn);
    $('cx-up').value = S.up; $('cx-down').value = S.down;
    $('cx-vol-check').checked = S.volOn;
    $('cx-grid').checked = S.grid !== false;
    $('cx-vol-op').value = S.volOpacity; $('cx-vol-op-val').textContent = S.volOpacity + '%';
  }
  function wireControls() {
    document.querySelectorAll('.cx-sym').forEach(function (b) {
      b.onclick = function () { S.sym = b.getAttribute('data-sym'); save(); syncControls(); load(); };
    });
    document.querySelectorAll('.cx-tf').forEach(function (b) {
      b.onclick = function () { S.tf = b.getAttribute('data-tf'); save(); syncControls(); load(); };
    });
    $('cx-fit').onclick = function () { chart.timeScale().fitContent(); };
    $('cx-latest').onclick = function () { chart.timeScale().scrollToRealTime(); };
    $('cx-fullscreen').onclick = function () {
      var el = $('page-chart');
      var fs = document.fullscreenElement || document.webkitFullscreenElement;
      if (!fs) { var r = el.requestFullscreen || el.webkitRequestFullscreen; if (r) r.call(el); }
      else { var x = document.exitFullscreen || document.webkitExitFullscreen; if (x) x.call(document); }
    };
    ['fullscreenchange', 'webkitfullscreenchange'].forEach(function (ev) {
      document.addEventListener(ev, function () { setTimeout(sizeCanvas, 80); });
    });
    $('theme-toggle').onclick = function () {
      S.theme = S.theme === 'light' ? 'dark' : 'light'; save(); applyTheme();
    };
    $('cx-settings-btn').onclick = function () {
      $('cx-settings').classList.toggle('open'); $('cx-settings-btn').classList.toggle('active');
    };
    function setVol(on) { S.volOn = on; save(); applyVolVisible(); syncControls(); }
    $('cx-vol-toggle').onclick = function () { setVol(!S.volOn); };
    $('cx-vol-check').onchange = function () { setVol(this.checked); };
    $('cx-grid').onchange = function () {
      S.grid = this.checked; save();
      chart.applyOptions({ grid: { vertLines: { visible: S.grid }, horzLines: { visible: S.grid } } });
    };
    $('cx-up').oninput = function () { S.up = this.value; save(); applyColors(); refreshLegend(); };
    $('cx-down').oninput = function () { S.down = this.value; save(); applyColors(); refreshLegend(); };
    $('cx-vol-op').oninput = function () {
      S.volOpacity = +this.value; $('cx-vol-op-val').textContent = S.volOpacity + '%';
      save(); if (bars.length) volume.setData(toVol(bars));
    };
  }
  function refreshLegend() { if (bars.length) renderLegend(bars[bars.length - 1]); }

  // ─────────────────────── live (WebSocket) ───────────────────────
  var reloadT = 0;
  function reloadSoon() { clearTimeout(reloadT); reloadT = setTimeout(load, 1500); }

  function connectLive() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var delay = 2000, everOpen = false;
    function open() {
      var ws = new WebSocket(proto + '//' + location.host + '/ws');
      ws.onopen = function () {
        console.log('[live] WebSocket connected');
        delay = 2000;                          // reset backoff
        if (everOpen) load();                  // resync after a drop (catch missed bars/levels)
        everOpen = true;
      };
      ws.onmessage = onLiveMsg;
      ws.onclose = function () {
        console.warn('[live] WebSocket closed — reconnecting in ' + Math.round(delay / 1000) + 's');
        setTimeout(open, delay);
        delay = Math.min(delay * 1.8, 30000);  // exponential backoff, cap 30s
      };
      ws.onerror = function () { try { ws.close(); } catch (e) {} };
    }
    open();
  }
  function onLiveMsg(m) {
    var ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
    if (ev.sym && ev.sym !== S.sym) return;
    if (ev.e === 'tick' || ev.e === 'bar_close') applyLiveBar(ev);
    else if (ev.e === 'level_new') { scheduleLvFetch(); scheduleMid(); }   // refetch (confirmed only)
    else if (ev.e === 'level_swept') {
      var lv = levelData.levels.find(function (l) { return l.id === ev.id; });
      if (lv) { lv.swept = true; if (ev.at) lv.end = ev.at; redrawLevels(); }
      scheduleMid();
    } else if (ev.e === 'reconciled') { scheduleLvFetch(); scheduleMid(); }
    else if (ev.e === 'model_score') applyScore(ev);
  }
  function applyLiveBar(ev) {
    var b = ev.bar;
    if (ev.e === 'tick' && b) formingTime = b.time;   // in-progress bar → blank in score pane
    if (S.tf === '1m') {
      candle.update({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close });
      volume.update({ time: b.time, value: b.volume, color: volColor(b) });
      if (bars.length && bars[bars.length - 1].time === b.time) bars[bars.length - 1] = b;
      else if (!bars.length || b.time > bars[bars.length - 1].time) bars.push(b);
      renderLegend(b);
      redrawLevels();
      tickCountdown();
    } else if (ev.e === 'bar_close') {
      reloadSoon();                              // fold the closed 1m into the higher tf
    } else if (bars.length) {                    // higher-tf tick: nudge the forming bar
      var last = bars[bars.length - 1];
      last.close = b.close; last.high = Math.max(last.high, b.high); last.low = Math.min(last.low, b.low);
      candle.update({ time: last.time, open: last.open, high: last.high, low: last.low, close: last.close });
    }
  }

  // ─────────────────────── liquidity levels ───────────────────────
  function lvKindsForSpan(days) {        // which kinds to request for this zoom
    var kinds = {};
    LVDEF.forEach(function (t) {
      if (!LV.types[t.key].on) return;
      if (t.tier <= 2) kinds[t.kind] = 1;
      else if (t.kind === 'killzone') { if (days <= 60) kinds.killzone = 1; }
      else if (t.kind === 'ib') { if (days <= 60) kinds.ib = 1; }
      else if (days <= 12) kinds[t.kind] = 1;   // macro / between only when zoomed in
    });
    return Object.keys(kinds);
  }
  function lvCaps() {
    var kz = Math.max(LV.types.kz_asia.ext, LV.types.kz_london.ext, LV.types.kz_nyam.ext, LV.types.kz_nypm.ext);
    return ['day:' + LV.types.day.ext, 'ib:' + LV.types.ib.ext, 'killzone:' + kz,
            'macro:' + LV.types.macro.ext, 'between:' + LV.types.between.ext].join(',');
  }
  function lvEnabledKinds() {            // every enabled kind, ignoring zoom
    var kinds = {};
    LVDEF.forEach(function (t) { if (LV.types[t.key].on) kinds[t.kind] = 1; });
    return Object.keys(kinds);
  }
  // Levels are fetched for a window wider than the screen; panning/zooming
  // within that buffer (and the same symbol/types/caps) just redraws — no call.
  var lvCache = { key: null, from: 0, to: 0 };
  function lvKey(kinds) {
    return [S.sym, LV.unsweptOnly ? 'u' : 'a', LV.mode, LV.closestN,
            kinds.slice().sort().join(','), lvCaps()].join('|');
  }
  function maybeFetchLevels() {
    if (!$('cx-overlay') || !bars.length) {
      levelData = { levels: [], zones: [] }; lvCache = { key: null, from: 0, to: 0 };
      redrawLevels(); return;
    }
    var vr = chart.timeScale().getVisibleRange(); if (!vr) return;
    var vfrom = Math.floor(vr.from), vto = Math.ceil(vr.to);
    var closest = LV.unsweptOnly && LV.mode === 'closest';
    var kinds = closest ? lvEnabledKinds() : lvKindsForSpan((vto - vfrom) / 86400);
    if (!kinds.length) {
      levelData = { levels: [], zones: [] }; lvCache = { key: null, from: 0, to: 0 };
      redrawLevels(); return;
    }
    var key = lvKey(kinds);
    if (closest) {                          // nearest set is range-independent
      if (key !== lvCache.key) doFetchLevels(kinds, key, vfrom, vto, true);
      return;
    }
    if (key === lvCache.key && vfrom >= lvCache.from && vto <= lvCache.to) return;  // in buffer
    var pad = Math.max(vto - vfrom, 7200);  // fetch ±one screen-width (min 2h)
    doFetchLevels(kinds, key, vfrom - pad, vto + pad, false);
  }
  function doFetchLevels(kinds, key, from, to, closest) {
    var my = ++lvReq;
    var u = new URL('/api/levels/' + S.sym, location.origin);
    u.searchParams.set('from', Math.floor(from)); u.searchParams.set('to', Math.ceil(to));
    u.searchParams.set('types', kinds.join(',')); u.searchParams.set('caps', lvCaps());
    if (closest) u.searchParams.set('closest', Math.max(1, LV.closestN | 0));
    fetch(u).then(function (r) { return r.json(); }).then(function (d) {
      if (my !== lvReq) return;
      levelData = { levels: d.levels || [], zones: d.zones || [] };
      lvCache = closest ? { key: key, from: -Infinity, to: Infinity }
                        : { key: key, from: Math.floor(from), to: Math.ceil(to) };
      redrawLevels();
    }).catch(function () {});
  }
  function scheduleLvFetch() { clearTimeout(lvFetchT); lvFetchT = setTimeout(maybeFetchLevels, 300); }

  // ─────────────────── model signals (Stage-1 score + fire) ───────────────────
  // Historical signals (current model scored on every past bar) come from
  // /api/scores per visible range; live signals arrive on the `model_score` WS
  // event as bars close. Drawn as fire markers on candles + a p(up) oscillator in
  // a bottom band of the synced overlay. 1-minute timeframe only (model cadence).
  var VOL_FRAC = 0.11;                         // volume share of the plot height
  var SCORE = (function () {
    var d = { on: true, band: 0.26, legendOp: 100, auto: true, view: { lo: 0, hi: 1 },
      lines: { mid:  { on: true, color: '#8892a4', width: 1, style: 'dashed' },
               up:   { on: true, color: '#26a69a', width: 1, style: 'dotted' },
               down: { on: true, color: '#ef5350', width: 1, style: 'dotted' } } };
    var s = JSON.parse(localStorage.getItem('cxscore') || 'null');
    if (s) {
      d.on = s.on !== false;
      if (s.band) d.band = s.band;
      if (s.legendOp != null) d.legendOp = s.legendOp;
      if (typeof s.auto === 'boolean') d.auto = s.auto;
      if (s.view && typeof s.view.lo === 'number') d.view = s.view;
      ['mid', 'up', 'down'].forEach(function (k) { if (s.lines && s.lines[k]) Object.assign(d.lines[k], s.lines[k]); });
    }
    return d;
  })();
  function saveScore() { localStorage.setItem('cxscore', JSON.stringify(SCORE)); scheduleServerSave(); }
  var scoreView = SCORE.view;                 // manual y-zoom (persisted, used when SCORE.auto=false)
  var curView = { lo: 0, hi: 1 };             // the view actually drawn (auto or manual)
  // Auto-fit: visible data + the threshold lines fill ~75% of the pane; the
  // threshold span is always included so we never over-zoom.
  function computeAutoView(rightX) {
    var lo = Math.min(scoreGate.short, 0.5), hi = Math.max(scoreGate.long, 0.5);
    for (var i = 0; i < scoreData.length; i++) {
      var s = scoreData[i]; if (s.time === formingTime) continue;
      var x = timeToX(s.time); if (x == null || x < 0 || x > rightX) continue;
      if (s.p_up < lo) lo = s.p_up; if (s.p_up > hi) hi = s.p_up;
    }
    var pad = Math.max(hi - lo, 0.06) * ((1 / 0.75) - 1) / 2;   // data ≈ 75% of pane
    return { lo: Math.max(0, lo - pad), hi: Math.min(1, hi + pad) };
  }
  // up/down line VALUES come from the model's own gate (server), not editable;
  // mid is fixed at 0.5. Only line on/off + color/width/style are user settings.
  var scoreGate = { long: 0.65, short: 0.35 };
  var formingTime = null;                     // ts of the in-progress bar (no signal yet)
  var firedTimes = {};                        // {sym: [ts,...]} for the prev/next signal arrows
  var scoreData = [], scoreCache = { key: null, from: 0, to: 0 }, scoreReq = 0, scoreFetchT = 0;
  var scoreGeom = null;                        // {y0,y1,band} px of the pane (for the y-axis drag zone)

  // Reserve a dedicated bottom band for the score pane: push price + volume up so
  // candles/levels never overlap it. Restore normal layout when off / not on 1m.
  function applyScoreLayout() {
    if (!chart || !candle || !volume) return;
    var on = SCORE.on && S.tf === '1m';
    candle.priceScale().applyOptions({ scaleMargins: on
      ? { top: 0.05, bottom: SCORE.band + VOL_FRAC } : { top: 0.08, bottom: 0.02 } });
    volume.priceScale().applyOptions({ scaleMargins: on
      ? { top: 1 - SCORE.band - VOL_FRAC, bottom: SCORE.band } : { top: 0.82, bottom: 0 } });
  }
  function applyLegendOpacity() {   // controls the liquidity-zone legend (bottom)
    var el = $('cx-label-legend'); if (el) el.style.opacity = (SCORE.legendOp / 100).toFixed(2);
  }

  function scheduleScoreFetch() { clearTimeout(scoreFetchT); scoreFetchT = setTimeout(maybeFetchScores, 300); }
  function maybeFetchScores() {
    if (!SCORE.on || S.tf !== '1m' || !bars.length || !candle) {
      scoreData = []; scoreCache = { key: null, from: 0, to: 0 };
      if (candle) candle.setMarkers([]); redrawLevels(); return;
    }
    var vr = chart.timeScale().getVisibleRange(); if (!vr) return;
    var vfrom = Math.floor(vr.from), vto = Math.ceil(vr.to);
    if (S.sym === scoreCache.key && vfrom >= scoreCache.from && vto <= scoreCache.to) return;
    var pad = Math.max(vto - vfrom, 7200), my = ++scoreReq;
    var u = new URL('/api/scores/' + S.sym, location.origin);
    u.searchParams.set('from', Math.floor(vfrom - pad)); u.searchParams.set('to', Math.ceil(vto + pad));
    fetch(u).then(function (r) { return r.json(); }).then(function (d) {
      if (my !== scoreReq) return;
      scoreData = d.scores || [];
      if (d.gate) scoreGate = d.gate;
      scoreCache = { key: S.sym, from: Math.floor(vfrom - pad), to: Math.ceil(vto + pad) };
      updateScoreMarkers(); redrawLevels();
    }).catch(function () {});
  }
  function updateScoreMarkers() {
    if (!candle) return;
    if (!SCORE.on || S.tf !== '1m' || !bars.length) { candle.setMarkers([]); return; }
    var lo = bars[0].time, hi = bars[bars.length - 1].time, mk = [];
    for (var i = 0; i < scoreData.length; i++) {
      var s = scoreData[i];
      if (!s.fire || s.time < lo || s.time > hi) continue;
      mk.push({ time: s.time, position: s.side === 1 ? 'belowBar' : 'aboveBar',
                color: s.side === 1 ? S.up : S.down, shape: s.side === 1 ? 'arrowUp' : 'arrowDown' });
    }
    candle.setMarkers(mk);
  }
  function applyScore(ev) {
    if (S.tf !== '1m') return;
    var row = { time: ev.time, p_up: ev.p_up, side: ev.side, fire: ev.fire };
    // replace any existing entry for this bar (avoids a duplicate column/marker —
    // the bar may already be present from the fetched history/live file); live wins.
    var i = scoreData.length - 1;
    for (; i >= 0; i--) if (scoreData[i].time === ev.time) break;
    if (i >= 0) scoreData[i] = row; else scoreData.push(row);
    updateScoreMarkers(); redrawLevels();
  }
  function lineDash(style) { return style === 'dotted' ? [2, 3] : style === 'dashed' ? [6, 4] : []; }
  function niceTicks(lo, hi) {
    var out = [], step = (hi - lo) >= 0.5 ? 0.25 : (hi - lo) >= 0.2 ? 0.1 : 0.05;
    for (var v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v * 100) / 100);
    return out;
  }
  function positionScoreAxis() {
    var ax = $('cx-score-axis'); if (!ax) return;
    if (!scoreGeom || !SCORE.on || S.tf !== '1m') { ax.style.display = 'none'; return; }
    ax.style.display = 'block';
    ax.style.top = Math.round(scoreGeom.y0) + 'px';
    ax.style.height = Math.round(scoreGeom.band) + 'px';
  }
  function redrawScore(ctx, c, rightX) {
    if (!SCORE.on || S.tf !== '1m') { scoreGeom = null; positionScoreAxis(); return; }
    var axisH = chart.timeScale().height() || 28;
    var plotH = c.clientHeight - axisH;
    var band = plotH * SCORE.band, y1 = plotH, y0 = plotH - band;
    scoreGeom = { y0: y0, y1: y1, band: band }; positionScoreAxis();
    var view = SCORE.auto ? computeAutoView(rightX) : scoreView;   // auto-fit by default
    curView = view;
    var lo = view.lo, hi = view.hi, rng = (hi - lo) || 1;
    var yFor = function (p) { return y1 - ((p - lo) / rng) * band; };
    var bg = S.theme === 'light' ? '#ffffff' : '#080a12';
    // opaque background over the band — hides any candle/level that bleeds in
    ctx.fillStyle = bg; ctx.fillRect(0, y0, rightX, band);
    ctx.setLineDash([]); ctx.lineWidth = 1; ctx.strokeStyle = hexToRgba('#8892a4', 0.4);
    ctx.beginPath(); ctx.moveTo(0, y0); ctx.lineTo(rightX, y0); ctx.stroke();   // top separator
    // reference lines: mid fixed 0.5; up/down = the model's own gate (not editable)
    ['mid', 'up', 'down'].forEach(function (k) {
      var ln = SCORE.lines[k];
      var val = k === 'mid' ? 0.5 : k === 'up' ? scoreGate.long : scoreGate.short;
      if (!ln.on || val < lo || val > hi) return;
      var yy = yFor(val);
      ctx.strokeStyle = ln.color; ctx.lineWidth = ln.width || 1; ctx.setLineDash(lineDash(ln.style));
      ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(rightX, yy); ctx.stroke();
    });
    ctx.setLineDash([]);
    // p(up) columns from 0.5 (fired = solid, else faint); skip the forming bar
    var mid05 = yFor(0.5);
    for (var i = 0; i < scoreData.length; i++) {
      var s = scoreData[i]; if (s.time === formingTime) continue;
      var x = timeToX(s.time); if (x == null || x < 0 || x > rightX) continue;
      var yv = Math.max(y0, Math.min(y1, yFor(s.p_up))), up = s.p_up >= 0.5;
      ctx.fillStyle = s.fire ? (s.side === 1 ? S.up : S.down) : hexToRgba(up ? S.up : S.down, 0.3);
      ctx.fillRect(x - 0.6, Math.min(mid05, yv), 1.4, Math.max(1, Math.abs(yv - mid05)));
    }
    // LEFT y-axis labels (chips so columns don't obscure them) + pane title
    ctx.font = '9px system-ui'; ctx.textAlign = 'left';
    niceTicks(lo, hi).forEach(function (p) {
      var yy = yFor(p); if (yy < y0 - 1 || yy > y1 + 1) return;
      ctx.fillStyle = bg; ctx.fillRect(0, yy - 6, 22, 12);
      ctx.fillStyle = hexToRgba('#8892a4', 0.95); ctx.fillText(p.toFixed(2), 2, yy + 3);
    });
    ctx.fillStyle = hexToRgba('#8892a4', 0.9); ctx.fillText('p(up)', 26, y0 + 11);
  }
  function wireScore() {
    var b = $('cx-sig-toggle');
    if (b) {
      b.classList.toggle('active', SCORE.on);
      b.onclick = function () {
        SCORE.on = !SCORE.on; saveScore();
        b.classList.toggle('active', SCORE.on);
        applyScoreLayout();
        if (SCORE.on) maybeFetchScores();
        else { if (candle) candle.setMarkers([]); scoreData = []; scoreCache = { key: null, from: 0, to: 0 }; positionScoreAxis(); redrawLevels(); }
      };
    }
    applyLegendOpacity(); wireScoreAxis(); wireScoreNav(); wireScoreSettings();
  }
  // drag the LEFT y-axis to zoom the score scale; double-click resets to 0–1
  function wireScoreAxis() {
    var ax = $('cx-score-axis'); if (!ax || ax._wired) return; ax._wired = true;
    var dragging = false, lastY = 0, lastTap = 0;
    function beginDrag(y) {
      // first manual drag: seed the manual range from the current auto view, switch off auto
      if (SCORE.auto) { scoreView.lo = curView.lo; scoreView.hi = curView.hi; SCORE.auto = false; }
      dragging = true; lastY = y;
    }
    function zoom(dy) {
      var c = (scoreView.lo + scoreView.hi) / 2, half = (scoreView.hi - scoreView.lo) / 2;
      half = Math.max(0.03, Math.min(0.5, half * (1 + dy * 0.004)));
      scoreView.lo = Math.max(0, c - half); scoreView.hi = Math.min(1, c + half);
      redrawLevels();
    }
    function resetAuto() { SCORE.auto = true; saveScore(); redrawLevels(); }
    // mouse
    ax.addEventListener('mousedown', function (e) { beginDrag(e.clientY); e.preventDefault(); });
    window.addEventListener('mousemove', function (e) { if (dragging) zoom(e.movementY); });
    window.addEventListener('mouseup', function () { if (dragging) { dragging = false; saveScore(); } });
    ax.addEventListener('dblclick', resetAuto);
    // touch (mobile) — double-tap resets to auto-fit
    ax.addEventListener('touchstart', function (e) {
      var now = Date.now();
      if (now - lastTap < 300) { resetAuto(); lastTap = 0; e.preventDefault(); return; }
      lastTap = now;
      if (e.touches.length) { beginDrag(e.touches[0].clientY); e.preventDefault(); }
    }, { passive: false });
    ax.addEventListener('touchmove', function (e) {
      if (!dragging || !e.touches.length) return;
      var y = e.touches[0].clientY; zoom(y - lastY); lastY = y; e.preventDefault();
    }, { passive: false });
    ax.addEventListener('touchend', function () { if (dragging) { dragging = false; saveScore(); } });
  }
  // prev/next: scroll to the nearest fired signal within the loaded bars
  function loadFired(sym) {
    var u = new URL('/api/scores/' + sym, location.origin); u.searchParams.set('fired', '1');
    fetch(u).then(function (r) { return r.json(); }).then(function (d) {
      firedTimes[sym] = (d.scores || []).map(function (s) { return s.time; });
    }).catch(function () {});
  }
  function navSig(dir) {
    var arr = firedTimes[S.sym]; if (!arr || !arr.length || !bars.length) return;
    var vr = chart.timeScale().getVisibleRange(); if (!vr) return;
    var c = (vr.from + vr.to) / 2, w = vr.to - vr.from;
    var lo = bars[0].time, hi = bars[bars.length - 1].time, target = null, i;
    if (dir > 0) { for (i = 0; i < arr.length; i++) if (arr[i] > c + 1 && arr[i] <= hi) { target = arr[i]; break; } }
    else { for (i = arr.length - 1; i >= 0; i--) if (arr[i] < c - 1 && arr[i] >= lo) { target = arr[i]; break; } }
    if (target == null) return;
    chart.timeScale().setVisibleRange({ from: Math.round(target - w / 2), to: Math.round(target + w / 2) });
  }
  function wireScoreNav() {
    var p = $('cx-sig-prev'), n = $('cx-sig-next');
    if (p) p.onclick = function () { navSig(-1); };
    if (n) n.onclick = function () { navSig(1); };
  }
  function wireScoreSettings() {
    var band = $('cx-score-band');
    if (band) {
      band.value = Math.round(SCORE.band * 100); $('cx-score-band-val').textContent = Math.round(SCORE.band * 100) + '%';
      band.oninput = function () { SCORE.band = Math.max(0.1, Math.min(0.5, +this.value / 100)); $('cx-score-band-val').textContent = Math.round(SCORE.band * 100) + '%'; saveScore(); applyScoreLayout(); redrawLevels(); };
    }
    var lop = $('cx-leg-op');
    if (lop) {
      lop.value = SCORE.legendOp; $('cx-leg-op-val').textContent = SCORE.legendOp + '%';
      lop.oninput = function () { SCORE.legendOp = +this.value; $('cx-leg-op-val').textContent = SCORE.legendOp + '%'; saveScore(); applyLegendOpacity(); };
    }
    ['mid', 'up', 'down'].forEach(function (k) {
      var ln = SCORE.lines[k];
      var on = $('cx-sl-' + k + '-on'), col = $('cx-sl-' + k + '-color'),
          w = $('cx-sl-' + k + '-w'), st = $('cx-sl-' + k + '-style');
      if (on) { on.checked = ln.on; on.onchange = function () { ln.on = this.checked; saveScore(); redrawLevels(); }; }
      if (col) { col.value = ln.color; col.oninput = function () { ln.color = this.value; saveScore(); redrawLevels(); }; }
      if (w) { w.value = ln.width; w.oninput = function () { ln.width = Math.max(1, +this.value || 1); saveScore(); redrawLevels(); }; }
      if (st) { st.value = ln.style; st.onchange = function () { ln.style = this.value; saveScore(); redrawLevels(); }; }
    });
  }

  // ── current midpoint (IBM): dotted line midway between the nearest unswept
  //    level above and the nearest unswept level below the latest price ──
  var midLine = null, midReq = 0, midT = 0;
  function midColor() { return S.theme === 'light' ? '#5b6b85' : '#c8cee0'; }
  function clearMid() { if (midLine && candle) { try { candle.removePriceLine(midLine); } catch (e) {} } midLine = null; }
  function updateMidpoint() {
    if (!LV.midOn || !candle) { clearMid(); return; }
    var my = ++midReq;
    var u = new URL('/api/midpoint/' + S.sym, location.origin);
    var kinds = lvEnabledKinds(); if (kinds.length) u.searchParams.set('types', kinds.join(','));
    fetch(u).then(function (r) { return r.json(); }).then(function (d) {
      if (my !== midReq || !LV.midOn) return;
      if (d && d.mid != null) {
        var dotted = (window.LightweightCharts && LightweightCharts.LineStyle) ? LightweightCharts.LineStyle.Dotted : 1;
        var opts = { price: d.mid, color: midColor(), lineWidth: 1, lineStyle: dotted,
                     axisLabelVisible: true, title: 'mid' };
        if (midLine) midLine.applyOptions(opts); else midLine = candle.createPriceLine(opts);
      }
      // null mid (no confirmed bracket, e.g. fast trend) -> keep the last line (freeze)
    }).catch(function () {});
  }
  function scheduleMid() { clearTimeout(midT); midT = setTimeout(updateMidpoint, 250); }

  function labelText(lv) { return lv.tags.map(function (t) { return ABBR[t] || t; }).join('·'); }
  function drawLabels(ctx, labels) {
    labels.sort(function (a, b) { return a.y - b.y; });
    ctx.font = '10px -apple-system,Segoe UI,system-ui,sans-serif';
    ctx.textAlign = 'right'; ctx.textBaseline = 'bottom';   // sit just above the line
    var lastY = -1e9;
    labels.forEach(function (l) {
      var y = l.y; if (y - lastY < 11) y = lastY + 11; lastY = y;
      ctx.fillStyle = l.color; ctx.fillText(l.text, l.x, y - 2);
    });
  }
  function dashFor(style, swept, mid) {
    if (mid) return [2, 3];
    if (swept) return [5, 4];               // swept → dashed (matches the script)
    if (style === 'dashed') return [6, 4];
    if (style === 'dotted') return [2, 3];
    return [];
  }
  function redrawLevels() {
    var c = $('cx-overlay'); if (!c) return;
    var ctx = c.getContext('2d');
    ctx.clearRect(0, 0, c.clientWidth, c.clientHeight);
    if (!bars.length) return;
    var W = c.clientWidth;
    var rightX = chart.timeScale().width();          // right edge of the plot area
    if (!rightX || rightX <= 0) {
      var range = chart.timeScale().getVisibleLogicalRange();
      rightX = range ? chart.timeScale().logicalToCoordinate(range.to) : W;
    }
    if (rightX == null) rightX = W;
    var lastTime = bars[bars.length - 1].time;
    var closest = LV.unsweptOnly && LV.mode === 'closest';  // backend returns nearest N

    if (!closest) {
      levelData.zones.forEach(function (z) {     // shaded session/macro boxes
        var key = z.kind === 'killzone' ? 'kz_' + z.session : z.kind;
        var st = LV.types[key]; if (!st || !st.on || !st.box) return;
        var x1 = timeToX(z.start), x2 = timeToX(z.end), y1 = priceToY(z.hi), y2 = priceToY(z.lo);
        if (x1 == null || x2 == null || y1 == null || y2 == null) return;
        ctx.fillStyle = hexToRgba(st.boxColor || st.color, (st.boxOpacity || 0) / 100);
        ctx.fillRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
      });
    }

    var labels = [];
    levelData.levels.forEach(function (lv) {
      var key = lv.kind === 'killzone' ? 'kz_' + lv.session : lv.kind;
      var st = LV.types[key]; if (!st || !st.on) return;
      if (LV.unsweptOnly && lv.swept) return;     // hide swept when unswept-only
      if (lv.side === 'mid' && !st.mid) return;   // (closest mode returns no mids)
      var y = priceToY(lv.price); if (y == null) return;
      // Anchor at the bar that made the high/low (lv.start), extending forward.
      var x1 = timeToX(lv.start); if (x1 == null) x1 = 0;
      // Closest mode extends every level fully to the right edge.
      var x2 = closest ? rightX
             : (lv.end == null || lv.end > lastTime) ? rightX : timeToX(lv.end);
      if (x2 == null) x2 = rightX;
      if (x2 <= x1) return;            // forward only — never draw a backward segment
      if (x2 < 0 || x1 > W) return;
      var isPrim = lv.primary && lv.kind === 'macro';
      var col = lv.side === 'mid' ? (st.midColor || st.color)
              : (isPrim && st.primaryColor ? st.primaryColor : st.color);
      ctx.strokeStyle = hexToRgba(col, (st.opacity || 100) / 100);
      ctx.lineWidth = isPrim ? st.width + 1 : st.width;
      ctx.setLineDash(dashFor(st.style, lv.swept, lv.side === 'mid'));
      ctx.beginPath(); ctx.moveTo(x1, y); ctx.lineTo(x2, y); ctx.stroke();
      ctx.setLineDash([]);
      if ((closest || st.label) && lv.side !== 'mid')
        labels.push({ y: y, x: Math.min(x2, rightX) - 4, text: labelText(lv), color: col });
    });
    drawLabels(ctx, labels);
    redrawScore(ctx, c, rightX);
  }

  // ── settings panel ──
  function lvSw(key, prop, v) { return '<label class="cx-sw"><input type="checkbox" data-key="' + key + '" data-prop="' + prop + '"' + (v ? ' checked' : '') + '><span class="cx-sw-t"></span></label>'; }
  function lvRow(lbl, ctrl) { return '<label>' + lbl + '</label>' + ctrl; }
  function lvColor(key, prop, v) { return '<input type="color" data-key="' + key + '" data-prop="' + prop + '" value="' + v + '">'; }
  function lvRng(key, prop, v) { return '<input type="range" min="0" max="100" data-key="' + key + '" data-prop="' + prop + '" value="' + v + '">'; }
  function lvNum(key, prop, v, mn, mx) { return '<input type="number" min="' + mn + '" max="' + mx + '" data-key="' + key + '" data-prop="' + prop + '" value="' + (v == null ? '' : v) + '">'; }
  function lvStyleSel(key, v) { return '<select data-key="' + key + '" data-prop="style">' + ['solid', 'dashed', 'dotted'].map(function (x) { return '<option' + (x === v ? ' selected' : '') + '>' + x + '</option>'; }).join('') + '</select>'; }
  function lvControls(t, s) {
    var h = lvRow('Color', lvColor(t.key, 'color', s.color)) + lvRow('Style', lvStyleSel(t.key, s.style)) +
            lvRow('Width', lvNum(t.key, 'width', s.width, 1, 5)) + lvRow('Opacity', lvRng(t.key, 'opacity', s.opacity)) +
            lvRow('Label', lvSw(t.key, 'label', s.label));
    if (t.key !== 'week') h += lvRow('Extend (d)', lvNum(t.key, 'ext', s.ext, 1, 365));
    if (t.kind === 'killzone' || t.kind === 'ib') h += lvRow('Box', lvSw(t.key, 'box', s.box)) + lvRow('Box color', lvColor(t.key, 'boxColor', s.boxColor)) + lvRow('Box opacity', lvRng(t.key, 'boxOpacity', s.boxOpacity));
    if (t.kind === 'macro' || t.kind === 'between' || t.kind === 'ib') h += lvRow('Midpoint', lvSw(t.key, 'mid', s.mid)) + lvRow('Mid color', lvColor(t.key, 'midColor', s.midColor));
    if (t.kind === 'macro') h += lvRow('Primary color', lvColor(t.key, 'primaryColor', s.primaryColor));
    return h;
  }
  function buildLevelsPanel() {
    var body = $('cx-lp-body'); if (!body) return;
    var uns = LV.unsweptOnly;
    var html =
      '<div class="cx-lt" style="background:none;border:none;padding:0;margin-bottom:6px"><div class="cx-lt-head">' +
      lvSw('__unswept', 'unswept', uns) + '<span class="cx-lt-name" style="font-weight:700">Unswept only</span></div></div>' +
      '<div class="cx-lp-mode" id="cx-mode-row"' + (uns ? '' : ' style="display:none"') + '>' +
      '<span class="cx-lp-mode-label">Show</span>' +
      '<select data-key="__mode" data-prop="mode">' +
        '<option value="all"' + (LV.mode === 'all' ? ' selected' : '') + '>All</option>' +
        '<option value="closest"' + (LV.mode === 'closest' ? ' selected' : '') + '>Closest to price</option>' +
      '</select></div>' +
      '<div class="cx-lp-mode" id="cx-closest-row"' + (uns && LV.mode === 'closest' ? '' : ' style="display:none"') + '>' +
      '<span class="cx-lp-mode-label">N per side</span>' +
      '<input type="number" min="1" max="50" data-key="__closestN" data-prop="closestN" value="' + LV.closestN + '"></div>' +
      '<div class="cx-lt" style="background:none;border:none;padding:0;margin:2px 0 6px"><div class="cx-lt-head">' +
      lvSw('__legend', 'legend', LV.legend) + '<span class="cx-lt-name">Show label legend</span></div></div>' +
      '<div class="cx-lt" style="background:none;border:none;padding:0;margin:2px 0 6px"><div class="cx-lt-head">' +
      lvSw('__mid', 'mid', LV.midOn) + '<span class="cx-lt-name">Current midpoint</span></div></div>';
    var last = '';
    LVDEF.forEach(function (t) {
      if (t.group !== last) { html += '<div class="cx-lg-title">' + t.group + '</div>'; last = t.group; }
      var s = LV.types[t.key];
      html += '<div class="cx-lt" data-card="' + t.key + '"><div class="cx-lt-head">' +
        lvSw(t.key, 'on', s.on) + '<span class="cx-lt-name">' + t.name + '</span>' +
        '<span class="cx-lt-tier">T' + t.tier + '</span><span class="cx-lt-caret" data-toggle="' + t.key + '">&#9654;</span>' +
        '</div><div class="cx-lt-body">' + lvControls(t, s) + '</div></div>';
    });
    body.innerHTML = html;
  }
  function buildLabelLegend() {
    var el = $('cx-label-legend'); if (!el) return;
    el.classList.toggle('on', !!LV.legend);
    if (!LV.legend) { el.innerHTML = ''; return; }
    var rows = '';
    LVDEF.forEach(function (t) {
      var closest = LV.unsweptOnly && LV.mode === 'closest';
      var s = LV.types[t.key]; if (!s.on || (!s.label && !closest)) return;
      var hk = t.kind === 'killzone' ? 'kz_' + t.session + '_high' : t.kind + '_high';
      var lk = t.kind === 'killzone' ? 'kz_' + t.session + '_low' : t.kind + '_low';
      var ab = (ABBR[hk] || '') + '/' + (ABBR[lk] || '');
      var nm = t.name.replace(/\s*\(.*\)\s*$/, '');     // drop the "(times)" suffix
      rows += '<div class="cx-ll-row"><span class="cx-ll-sw" style="background:' + s.color + '"></span>' +
              '<span class="cx-ll-ab" style="color:' + s.color + '">' + ab + '</span>' +
              '<span class="cx-ll-nm">' + nm + '</span></div>';
    });
    el.innerHTML = rows || '<div class="cx-ll-nm">No labelled levels</div>';
    applyLegendOpacity();
  }
  function onLvChange(e) {
    var el = e.target; if (!el.getAttribute) return;
    var key = el.getAttribute('data-key'), prop = el.getAttribute('data-prop');
    if (!key || !prop) return;
    var val = el.type === 'checkbox' ? el.checked
            : (el.type === 'range' || el.type === 'number') ? Number(el.value) : el.value;
    if (key === '__unswept') {
      LV.unsweptOnly = val; saveLV();
      var mr = $('cx-mode-row'); if (mr) mr.style.display = val ? '' : 'none';
      var cr0 = $('cx-closest-row'); if (cr0) cr0.style.display = (val && LV.mode === 'closest') ? '' : 'none';
      buildLabelLegend(); scheduleLvFetch(); return;
    }
    if (key === '__mode') {
      LV.mode = el.value; saveLV();
      var cr = $('cx-closest-row'); if (cr) cr.style.display = (LV.unsweptOnly && LV.mode === 'closest') ? '' : 'none';
      buildLabelLegend(); scheduleLvFetch(); return;
    }
    if (key === '__closestN') { LV.closestN = Math.max(1, Math.min(50, +el.value || 1)); saveLV(); scheduleLvFetch(); return; }
    if (key === '__legend') { LV.legend = val; saveLV(); buildLabelLegend(); return; }
    if (key === '__mid') { LV.midOn = val; saveLV(); updateMidpoint(); return; }
    LV.types[key][prop] = val; saveLV();
    if (prop === 'on' || prop === 'ext') { redrawLevels(); scheduleLvFetch(); }
    else redrawLevels();
    if (prop === 'on' || prop === 'label' || prop === 'color') buildLabelLegend();
  }
  function wireLevels() {
    $('cx-levels-btn').onclick = function () {
      var p = $('cx-levels-panel'); p.classList.toggle('open');
      $('cx-levels-btn').classList.toggle('active', p.classList.contains('open'));
    };
    $('cx-lp-x').onclick = function () { $('cx-levels-panel').classList.remove('open'); };
    var body = $('cx-lp-body');
    body.addEventListener('input', onLvChange);
    body.addEventListener('click', function (e) {
      var tog = e.target.getAttribute && e.target.getAttribute('data-toggle');
      if (tog) { var card = body.querySelector('[data-card="' + tog + '"]'); if (card) card.classList.toggle('open'); }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
