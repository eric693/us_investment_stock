/* 主題切換（亮色 / 暗色）
 * - 在 <head> 同步載入，畫面出現前就套用使用者的選擇，避免閃爍。
 * - 頁面裡的顏色一律寫成 CSS 變數；Plotly / TradingView 這類不懂 var() 的元件，
 *   由這裡把 'var(--x)' 解析成實際顏色，切換主題時自動重畫。
 */
(function () {
  var KEY = 'sl-theme';
  var root = document.documentElement;

  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function apply(m) {
    root.setAttribute('data-theme', m);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', m === 'light' ? '#f3f4f8' : '#07080d');
  }
  apply(stored() || 'dark');

  function mode() { return root.getAttribute('data-theme'); }
  function css(name) { return getComputedStyle(root).getPropertyValue(name).trim(); }
  function resolve(v) {
    if (typeof v !== 'string' || v.indexOf('var(--') < 0) return v;
    return v.replace(/var\((--[\w-]+)\)/g, function (_, n) { return css(n) || '#888'; });
  }
  function resolveDeep(o) {
    if (Array.isArray(o)) return o.map(resolveDeep);
    if (o && typeof o === 'object' && Object.getPrototypeOf(o) === Object.prototype) {
      var r = {};
      for (var k in o) r[k] = resolveDeep(o[k]);
      return r;
    }
    return resolve(o);
  }

  function set(m) {
    root.classList.add('theme-anim');
    apply(m);
    try { localStorage.setItem(KEY, m); } catch (e) {}
    setTimeout(function () { root.classList.remove('theme-anim'); }, 400);
    rethemePlotly();
    window.dispatchEvent(new CustomEvent('themechange', { detail: m }));
  }
  function toggle() { set(mode() === 'light' ? 'dark' : 'light'); }

  // ── Plotly：讓 layout / trace 裡的 var(--x) 生效，並在換主題時重畫 ──
  var plots = new Set();
  function patchPlotly() {
    var P = window.Plotly;
    if (!P || P.__themed) return;
    P.__themed = true;
    ['newPlot', 'react'].forEach(function (fn) {
      var orig = P[fn].bind(P);
      P['_orig_' + fn] = orig;
      P[fn] = function (gd, data, layout, config) {
        var el = typeof gd === 'string' ? document.getElementById(gd) : gd;
        if (el) { el.__themeSpec = { data: data, layout: layout, config: config }; plots.add(el); }
        return orig(el || gd, resolveDeep(data), resolveDeep(layout), config);
      };
    });
    var relayout = P.relayout.bind(P);
    P.relayout = function (gd, a, b) { return relayout(gd, typeof a === 'string' ? a : resolveDeep(a), resolve(b)); };
    var restyle = P.restyle.bind(P);
    P.restyle = function (gd, a, b, c) { return restyle(gd, typeof a === 'string' ? a : resolveDeep(a), resolveDeep(b), c); };
  }
  function rethemePlotly() {
    var P = window.Plotly;
    if (!P || !P.__themed) return;
    plots.forEach(function (el) {
      if (!document.body.contains(el)) { plots.delete(el); return; }
      var s = el.__themeSpec;
      Promise.resolve(P._orig_react(el, resolveDeep(s.data), resolveDeep(s.layout), s.config))
        .then(function () { if (el.offsetParent) P.Plots.resize(el); });
    });
  }
  patchPlotly();
  document.addEventListener('DOMContentLoaded', patchPlotly);

  // ── 互動：任何 [data-theme-toggle] 按鈕 ──
  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[data-theme-toggle]');
    if (t) { e.preventDefault(); toggle(); }
  });

  // ── 台股開收盤狀態（App bar 上的小燈號）──
  function marketTick() {
    var el = document.getElementById('mktChip');
    if (!el) return;
    var now = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Taipei' }));
    var hm = now.getHours() * 100 + now.getMinutes(), wd = now.getDay();
    var open = wd >= 1 && wd <= 5 && hm >= 900 && hm <= 1330;
    var pre = wd >= 1 && wd <= 5 && hm >= 830 && hm < 900;
    el.classList.toggle('open', open);
    el.querySelector('span').textContent = open ? '台股交易中' : pre ? '台股試撮' : '台股已收盤';
    var clock = el.querySelector('b');
    if (clock) clock.textContent = ('0' + now.getHours()).slice(-2) + ':' + ('0' + now.getMinutes()).slice(-2);
  }
  document.addEventListener('DOMContentLoaded', function () { marketTick(); setInterval(marketTick, 30000); });

  window.Theme = { mode: mode, set: set, toggle: toggle, css: css, resolve: resolve, resolveDeep: resolveDeep };
})();
