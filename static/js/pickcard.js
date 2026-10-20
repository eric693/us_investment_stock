// 個股分析頁的「潛力股評分」卡：讀 /api/picks/<market>/stock/<code>，不在評分範圍就隱藏
(function () {
  var css = '.pc-wrap{display:grid;grid-template-columns:150px 1fr 1.2fr;gap:18px;align-items:start}' +
    '@media(max-width:820px){.pc-wrap{grid-template-columns:1fr}}' +
    '.pc-score{font-family:"JetBrains Mono",monospace;font-size:2.4rem;font-weight:700;line-height:1}' +
    '.pc-score small{font-size:.8rem;color:var(--text-dim);font-weight:600;margin-left:4px}' +
    '.pc-rank{font-size:.78rem;color:var(--text-mid);margin-top:6px;line-height:1.6}' +
    '.pc-track{height:6px;border-radius:6px;background:var(--overlay-2);margin-top:10px;overflow:hidden}' +
    '.pc-track i{display:block;height:100%;border-radius:6px;background:var(--accent-grad)}' +
    '.pc-f{display:grid;grid-template-columns:96px 1fr 30px;gap:8px;align-items:center;font-size:.76rem;color:var(--text-mid);margin-bottom:7px}' +
    '.pc-f span:last-child{font-family:"JetBrains Mono",monospace;text-align:right;color:var(--text)}' +
    '.pc-f b{display:block;height:5px;border-radius:5px;background:var(--overlay-2);overflow:hidden}' +
    '.pc-f b i{display:block;height:100%;background:var(--accent);border-radius:5px}' +
    '.pc-rs{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}' +
    '.pc-rs span{font-size:.74rem;padding:2px 8px;border-radius:7px}' +
    '.pc-why{background:rgba(109,93,252,.1);border:1px solid rgba(109,93,252,.25)}' +
    '.pc-risk{background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.35)}' +
    '.pc-exp{font-size:.78rem;color:var(--text-mid);line-height:1.7}' +
    '.pc-exp b{font-family:"JetBrains Mono",monospace;color:var(--text)}' +
    '.pc-link{display:inline-block;margin-top:6px;font-size:.78rem;font-weight:700;color:var(--accent);text-decoration:none}';
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }
  function sgn(v) { return v == null ? '-' : '<span class="' + (v > 0 ? 'up' : v < 0 ? 'dn' : '') + '">' + (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%</span>'; }
  var seq = 0;
  window.loadPickCard = function (market, code) {
    var card = document.getElementById('pickCard'), panel = document.getElementById('pickPanel');
    if (!card || !panel) return;
    if (!document.getElementById('pc-css')) { var st = document.createElement('style'); st.id = 'pc-css'; st.textContent = css; document.head.appendChild(st); }
    var my = ++seq;
    card.classList.add('hidden');
    fetch('/api/picks/' + market + '/stock/' + encodeURIComponent(code)).then(function (r) { return r.ok ? r.json() : null; }).then(function (d) {
      if (!d || my !== seq) return;
      var names = {}; d.factors.forEach(function (f) { names[f.id] = f.name.replace('（反向）', ''); });
      var fbars = Object.keys(d.f).map(function (k) {
        var v = d.f[k];
        return '<div class="pc-f"><span>' + esc(names[k]) + '</span><b><i style="width:' + (v || 0) + '%"></i></b><span>' + (v == null ? '-' : v) + '</span></div>';
      }).join('');
      var rs = d.why.map(function (x) { return '<span class="pc-why">' + esc(x) + '</span>'; }).join('') +
               d.risk.map(function (x) { return '<span class="pc-risk">⚠ ' + esc(x) + '</span>'; }).join('');
      panel.innerHTML = '<div class="pc-wrap"><div><div class="pc-score">' + Math.round(d.score) + '<small>/ 100</small></div>' +
        '<div class="pc-track"><i style="width:' + d.score + '%"></i></div>' +
        '<div class="pc-rank">排名 <b>' + d.rank + '</b> / ' + d.universe + '・第 ' + d.decile + ' 組（10 = 最高）</div></div>' +
        '<div>' + fbars + '<div class="pc-exp" style="font-size:.72rem">各指標在全體中的百分位（100 = 最強）</div></div>' +
        '<div>' + (rs ? '<div class="pc-rs">' + rs + '</div>' : '') +
        '<div class="pc-exp">同分組過去 20 日：平均超額 ' + sgn(d.exp20) + '・上漲機率 <b>' + (d.win20 == null ? '-' : Math.round(d.win20)) + '%</b>・贏大盤 <b>' + (d.beat20 == null ? '-' : Math.round(d.beat20)) + '%</b>' +
        (d.stop ? '<br>停損參考（收盤 − 2×ATR）<b>' + d.stop + '</b>' : '') + '<br>資料至 ' + esc(d.end) + '</div>' +
        '<a class="pc-link" href="/picks?market=' + market + '">看完整排行與回測 →</a></div></div>';
      card.classList.remove('hidden');
    }).catch(function () {});
  };
})();
