/* PWA service worker：只快取 /static/ 與 Google 字型（先回快取、背景更新）。
 * 頁面、/api/ 一律走網路，不會看到舊報價；離線時頁面改回一張簡短的離線提示。
 * 改了快取策略就把 CACHE 版本號 +1，舊快取會在啟用時清掉。
 */
var CACHE = 'stocklens-static-v1';

self.addEventListener('install', function () { self.skipWaiting(); });

self.addEventListener('activate', function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
  }).then(function () { return self.clients.claim(); }));
});

var OFFLINE = '<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
  '<title>StockLens 離線</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#07080d;color:#e9edf5;' +
  'font-family:system-ui,"Noto Sans TC",sans-serif;text-align:center;padding:16px}p{color:#9aa3b6}button{margin-top:16px;padding:10px 22px;' +
  'border:0;border-radius:12px;background:linear-gradient(120deg,#6d5dfc,#22d3ee);color:#fff;font-weight:700;font-size:1rem}</style></head>' +
  '<body><div><h1>目前沒有網路</h1><p>連上網路後再重新整理，就能看到最新行情。</p><button onclick="location.reload()">重新整理</button></div></body></html>';

self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);

  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(function () {
      return new Response(OFFLINE, {headers: {'Content-Type': 'text/html; charset=utf-8'}});
    }));
    return;
  }

  var cacheable = (url.origin === self.location.origin && url.pathname.indexOf('/static/') === 0) ||
                  url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com';
  if (!cacheable) return;

  e.respondWith(caches.open(CACHE).then(function (cache) {
    return cache.match(req).then(function (hit) {
      var net = fetch(req).then(function (res) {
        if (res.ok || res.type === 'opaque') cache.put(req, res.clone());
        return res;
      });
      if (hit) { e.waitUntil(net.catch(function () {})); return hit; }
      return net;
    });
  }));
});
