/* 通知：App bar 的鈴鐺（推播失敗時亮紅點）＋這台裝置的推播訂閱（帳號設定頁用）。
 * 狀態來自 /api/notify/status；錯誤原因由伺服器整理好（LINE token 失效、額度用完…）。
 */
(function () {
  function json(url, opt) {
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || r.statusText);
        return j;
      });
    });
  }
  function post(url, body) {
    return json(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
  }
  function keyBytes(b64) {
    var s = atob((b64 + '='.repeat((4 - b64.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/'));
    var out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }

  var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  var standalone = (window.matchMedia && matchMedia('(display-mode: standalone)').matches) || navigator.standalone === true;
  var supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

  var N = {
    isIOS: isIOS, standalone: standalone, supported: supported,
    status: function () { return json('/api/notify/status'); },
    test: function () { return post('/api/notify/test'); },
    saveLine: function (token, uid, secret) {
      var b = {line_token: token, line_user_id: uid};
      if (secret !== undefined) b.line_secret = secret;
      return post('/api/tw/line/config', b);
    },
    lineBindCode: function () { return post('/api/notify/line/bind', {}); },

    // 這台裝置目前的訂閱（沒有就是 null）
    deviceSub: function () {
      if (!supported) return Promise.resolve(null);
      return navigator.serviceWorker.getRegistration('/').then(function (reg) {
        return reg ? reg.pushManager.getSubscription() : null;
      });
    },

    enableDevice: function () {
      if (!supported) {
        return Promise.reject(new Error(isIOS && !standalone
          ? 'iPhone 要先用 Safari 把 StockLens「加入主畫面」，再從主畫面打開才能開啟通知（需 iOS 16.4 以上）'
          : '這個瀏覽器不支援推播通知'));
      }
      return Notification.requestPermission().then(function (perm) {
        if (perm !== 'granted') {
          throw new Error(perm === 'denied'
            ? '通知權限被封鎖了，請到瀏覽器的網站設定（或手機的「設定 → 通知」）允許 StockLens 的通知'
            : '沒有允許通知');
        }
        return navigator.serviceWorker.register('/sw.js');
      }).then(function () {
        return Promise.all([navigator.serviceWorker.ready, N.status()]);
      }).then(function (r) {
        var reg = r[0], key = r[1].vapid_public;
        return reg.pushManager.getSubscription().then(function (sub) {
          return sub || reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: keyBytes(key)});
        });
      }).then(function (sub) {
        return post('/api/notify/subscribe', sub.toJSON());
      });
    },

    disableDevice: function () {
      return N.deviceSub().then(function (sub) {
        if (!sub) return null;
        var ep = sub.endpoint;
        return sub.unsubscribe().then(function () { return post('/api/notify/unsubscribe', {endpoint: ep}); });
      });
    },

    // 鈴鐺：有錯誤就亮紅點，滑過去看原因
    refreshBell: function () {
      var bell = document.getElementById('notifyBell');
      if (!bell) return Promise.resolve();
      return N.status().then(function (st) {
        var errs = st.errors || [];
        bell.classList.toggle('has-error', errs.length > 0);
        bell.title = errs.length ? '通知有問題：' + errs.join('；') : '通知設定';
        return st;
      }).catch(function () {});
    }
  };
  window.StockNotify = N;

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', N.refreshBell);
  else N.refreshBell();
})();
