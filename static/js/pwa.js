/* 安裝成 App（PWA）。
 * Android／電腦版 Chrome、Edge：攔下 beforeinstallprompt，按「安裝 App」時跳出瀏覽器的安裝視窗。
 * iPhone／iPad：Safari 沒有安裝 API，改顯示「分享 → 加入主畫面」的步驟；在 LINE 等 App 內建瀏覽器裡要先改用 Safari 開。
 * 頁面上任何 [data-pwa-install] 元素都會變成安裝按鈕，已安裝或無法安裝時自動隱藏。
 * 手機第一次逛到第二頁時在底部提示一次，按「之後再說」後 30 天不再出現。
 */
(function () {
  var ua = navigator.userAgent;
  var isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var inApp = /Line\/|FBAN|FBAV|Instagram|MicroMessenger/i.test(ua);
  var iosSafari = isIOS && /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS/.test(ua) && !inApp;
  var mobile = isIOS || /Android/i.test(ua);
  var deferred = null;

  function standalone() {
    return (window.matchMedia && matchMedia('(display-mode: standalone)').matches) || navigator.standalone === true;
  }
  function canInstall() { return !standalone() && (!!deferred || isIOS); }
  function store(k, v) {
    try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; }
  }

  var css = '.pwa-mask{position:fixed;inset:0;background:var(--scrim,rgba(0,0,0,.5));z-index:950;display:flex;align-items:flex-end;justify-content:center}' +
    '.pwa-sheet{width:min(440px,100%);background:var(--card-bg,#0e1119);color:var(--text,#e9edf5);border:1px solid var(--card-bd2,rgba(255,255,255,.12));' +
    'border-radius:20px 20px 0 0;padding:22px 22px calc(22px + env(safe-area-inset-bottom));box-shadow:0 -20px 50px rgba(0,0,0,.3)}' +
    '@media(min-width:600px){.pwa-mask{align-items:center}.pwa-sheet{border-radius:20px}}' +
    '.pwa-sheet h3{margin:0 0 4px;font-size:1.05rem;display:flex;align-items:center;gap:10px}' +
    '.pwa-sheet h3 img{width:40px;height:40px;border-radius:10px}' +
    '.pwa-sheet p{margin:0 0 14px;font-size:.84rem;color:var(--text-mid,#9aa3b6);line-height:1.7}' +
    '.pwa-sheet ol{margin:0 0 16px;padding-left:20px;font-size:.9rem;line-height:2}' +
    '.pwa-sheet .k{display:inline-flex;align-items:center;gap:4px;padding:1px 8px;border-radius:7px;border:1px solid var(--card-bd2,rgba(255,255,255,.15));font-weight:700}' +
    '.pwa-sheet .k svg{width:15px;height:15px}' +
    '.pwa-row{display:flex;gap:10px;justify-content:flex-end}' +
    '.pwa-btn{height:40px;padding:0 18px;border-radius:11px;border:1px solid var(--card-bd2,rgba(255,255,255,.15));background:var(--surface-1,#0a0d15);' +
    'color:var(--text,#e9edf5);font:inherit;font-weight:700;cursor:pointer}' +
    '.pwa-btn.pri{border:0;background:var(--accent-grad,linear-gradient(120deg,#6d5dfc,#22d3ee));color:#fff}' +
    '.pwa-bar{position:fixed;left:12px;right:12px;bottom:calc(12px + env(safe-area-inset-bottom));z-index:940;display:flex;align-items:center;gap:12px;' +
    'padding:12px 12px 12px 14px;border-radius:16px;background:var(--card-bg,#0e1119);border:1px solid var(--card-bd2,rgba(255,255,255,.12));' +
    'box-shadow:0 12px 40px rgba(0,0,0,.35);font-size:.84rem;color:var(--text,#e9edf5)}' +
    '.pwa-bar img{width:36px;height:36px;border-radius:9px;flex-shrink:0}' +
    '.pwa-bar div{flex:1;min-width:0;line-height:1.45}.pwa-bar small{display:block;color:var(--text-dim,#5d6678);font-size:.74rem}' +
    '.pwa-bar .x{background:none;border:0;color:var(--text-dim,#5d6678);font-size:1.2rem;padding:4px 6px;cursor:pointer}' +
    '[data-pwa-install][hidden]{display:none!important}';
  var ICON = '/static/icons/icon-192.png';
  var SHARE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M8 7l4-4 4 4"/><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/></svg>';
  var PLUS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="4"/><path d="M12 8v8M8 12h8"/></svg>';

  function injectCss() {
    if (document.getElementById('pwa-css')) return;
    var s = document.createElement('style'); s.id = 'pwa-css'; s.textContent = css; document.head.appendChild(s);
  }

  function sheet(html) {
    injectCss();
    var m = document.createElement('div'); m.className = 'pwa-mask';
    m.innerHTML = '<div class="pwa-sheet" role="dialog" aria-modal="true">' + html + '</div>';
    m.addEventListener('click', function (e) { if (e.target === m || e.target.closest('[data-close]')) m.remove(); });
    document.body.appendChild(m);
    return m;
  }

  function iosSteps() {
    var head = '<h3><img src="' + ICON + '" alt="">把 StockLens 加到主畫面</h3>';
    if (!iosSafari) {
      sheet(head + '<p>iPhone 只有 <b>Safari</b> 能安裝。請點右上角「⋯」或分享選單，選「<b>在 Safari 中打開</b>」，再照步驟加入主畫面。</p>' +
        '<div class="pwa-row"><button class="pwa-btn pri" data-close>知道了</button></div>');
      return;
    }
    sheet(head + '<p>安裝後從主畫面開啟，全螢幕使用，也能收到手機推播通知（iOS 16.4 以上）。</p>' +
      '<ol><li>點下方工具列的 <span class="k">' + SHARE + '分享</span></li>' +
      '<li>往下滑，選 <span class="k">' + PLUS + '加入主畫面</span></li>' +
      '<li>按右上角「<b>新增</b>」</li></ol>' +
      '<div class="pwa-row"><button class="pwa-btn pri" data-close>知道了</button></div>');
  }

  function install() {
    if (standalone()) return Promise.resolve('installed');
    if (deferred) {
      var d = deferred; deferred = null;
      d.prompt();
      return d.userChoice.then(function (c) { sync(); return c.outcome; });
    }
    if (isIOS) { iosSteps(); return Promise.resolve('ios'); }
    sheet('<h3><img src="' + ICON + '" alt="">安裝 StockLens</h3>' +
      '<p>請用 Chrome 或 Edge 開啟本站，點網址列右側的安裝圖示，或選單「⋮」→「安裝應用程式」／「加到主畫面」。</p>' +
      '<div class="pwa-row"><button class="pwa-btn pri" data-close>知道了</button></div>');
    return Promise.resolve('manual');
  }

  function sync() {
    var show = canInstall();
    document.querySelectorAll('[data-pwa-install]').forEach(function (el) {
      el.hidden = !show;
      if (!el.__pwa) { el.__pwa = true; el.addEventListener('click', function (e) { e.preventDefault(); bar(false); install(); }); }
    });
  }

  function bar(on) {
    var old = document.getElementById('pwa-bar');
    if (!on) { if (old) old.remove(); return; }
    if (old) return;
    injectCss();
    var b = document.createElement('div'); b.id = 'pwa-bar'; b.className = 'pwa-bar';
    b.innerHTML = '<img src="' + ICON + '" alt=""><div><b>安裝 StockLens App</b><small>從主畫面一鍵開啟，收得到推播通知</small></div>' +
      '<button class="pwa-btn pri" data-pwa-install>安裝</button><button class="x" aria-label="之後再說" title="之後再說">✕</button>';
    b.querySelector('.x').onclick = function () { store('pwa_snooze', String(Date.now())); bar(false); };
    document.body.appendChild(b);
    sync();
  }

  function maybeBar() {
    if (!mobile || !canInstall() || /^\/(login|about)/.test(location.pathname)) return;
    var snooze = +store('pwa_snooze') || 0;
    if (Date.now() - snooze < 30 * 864e5) return;
    var views = (+store('pwa_views') || 0) + 1;
    store('pwa_views', String(views));
    if (views >= 2) setTimeout(function () { bar(true); }, 1500);
  }

  injectCss();             // 讓 [hidden] 的安裝按鈕在任何樣式下都藏得住
  window.addEventListener('beforeinstallprompt', function (e) { e.preventDefault(); deferred = e; sync(); maybeBar(); });
  window.addEventListener('appinstalled', function () { deferred = null; bar(false); sync(); });
  document.addEventListener('DOMContentLoaded', function () { sync(); if (isIOS) maybeBar(); });

  window.StockPWA = {install: install, canInstall: canInstall, standalone: standalone, isIOS: isIOS};
})();
