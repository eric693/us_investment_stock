/* 個人資料存在伺服器、依帳號分開（投資組合、自選股、持倉、提醒…）。
 * 用法同 localStorage：UserStore.getItem(key) / setItem(key, value) / removeItem(key)。
 * 頁面載入時伺服器已把目前帳號的資料放在 window.__UKV，讀取是同步的；寫入會在背景存回伺服器。
 */
(function () {
  var KEYS = ['pflio_v2', 'wl', 'wl_tw', 'positions_tw', 'monitorList', 'monitor_settings',
              'priceAlerts', 'priceAlerts_tw', 'line_channel_token', 'line_user_id', 'tw_ma_settings'];
  var data = window.__UKV || {};
  var pending = {}, timer = null;

  function send(key, value) {
    var opt = { method: value === null ? 'DELETE' : 'PUT', headers: { 'Content-Type': 'application/json' },
                credentials: 'same-origin' };
    if (value !== null) opt.body = JSON.stringify({ value: value });
    if (opt.body && opt.body.length < 60000) opt.keepalive = true;   // 關頁時也能送出
    return fetch('/api/user/kv/' + encodeURIComponent(key), opt).catch(function () {});
  }
  function flush() {
    clearTimeout(timer); timer = null;
    var p = pending; pending = {};
    Object.keys(p).forEach(function (k) { send(k, p[k]); });
  }
  function schedule(key, value) {
    pending[key] = value;
    clearTimeout(timer);
    timer = setTimeout(flush, 300);
  }
  window.addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') flush(); });

  // 改版前存在這台瀏覽器的資料：只搬給原本的帳號一次；之後一律從瀏覽器清掉，
  // 換其他帳號登入同一台電腦時就看不到上一個人的資料。
  try {
    if (window.__UKV_MIGRATE) {
      KEYS.forEach(function (k) {
        var v = localStorage.getItem(k);
        if (v !== null && data[k] === undefined) { data[k] = v; send(k, v); }
      });
      send('_migrated', '1');
    }
    if (window.__UKV) KEYS.forEach(function (k) { localStorage.removeItem(k); });
  } catch (e) {}

  window.UserStore = {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
    setItem: function (k, v) { v = String(v); data[k] = v; schedule(k, v); },
    removeItem: function (k) { delete data[k]; schedule(k, null); },
  };
})();
