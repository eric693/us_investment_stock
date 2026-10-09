/* 操作說明：右側滑出的說明面板。
 * - 任何元素加上 data-help="章節 id" 點了就會打開並捲到該章節，例如 data-help="board.after"。
 * - App bar 的「？」打開目前頁面的全部說明（頁面以 <body data-help-page="board"> 指定）。
 */
(function () {
  var H = {};   // 章節 id → {t: 標題, b: HTML 內容}
  function sec(id, title, body) { H[id] = {t: title, b: body}; }
  function steps(list) { return '<ol>' + list.map(function (x) { return '<li>' + x + '</li>'; }).join('') + '</ol>'; }
  function note(x) { return '<p class="hp-note">' + x + '</p>'; }

  // ── 台股看盤 ──────────────────────────────────────────────
  sec('board.index', '大盤指數', '<p>上方兩張卡片是加權指數與櫃買指數：目前點數、漲跌、成交金額、近 60 日走勢，以及當天三大法人買賣超金額（億元）。</p>' +
    steps(['盤中約每 20 秒自動更新，右上角會顯示「LIVE」。', '點卡片進入指數的技術分析圖。']) +
    note('最上方導覽列的「加權」也會即時顯示加權指數，點它同樣可以看技術圖。'));
  sec('board.pulse', '市場寬度與排行', '<p>上漲／平盤／下跌家數、漲停／跌停家數、總成交量，以及漲幅、跌幅、成交量前 5 名。</p>' +
    steps(['點任一檔股票進入技術圖，並可在這 5 檔之間切換。']) + note('漲跌幅排行只計入成交量 200 張以上的股票，避免冷門股干擾。'));
  sec('board.all', '全部行情', '<p>所有上市櫃股票的報價表，加權／櫃買指數固定在最上面。</p>' +
    steps(['點欄位名稱排序，再點一次反向。', '左上搜尋框輸入代碼或名稱（輸入「加權」「大盤」可找到指數）；鍵盤按 <kbd>/</kbd> 可直接跳到搜尋框。',
      '用「全部市場／全部產業」篩選；「含 ETF」開關決定是否列出 ETF。', '點左側 ☆ 加入自選股（加到目前選的群組）。',
      '點代碼或名稱進入技術圖，之後可依這張表的順序一檔一檔往下看。', '表格一次顯示 200 列，捲到底會自動載入更多。']) +
    note('盤中每 20 秒更新；若右上角出現「⚠ 報價延遲」，代表證交所即時資料暫時沒有回應。'));
  sec('board.watch', '自選股（群組）', steps(['在「全部行情」點 ☆ 加入；再點一次 ★ 移除。',
      '上方群組列可切換群組、「＋ 新增群組」、「重新命名」、「刪除群組」。', '☆ 會加到目前選的群組；從某個群組點進技術圖，就會依該群組逐檔瀏覽。']) +
    note('自選股存在你的帳號，換電腦或手機登入同一帳號也看得到；其他帳號看不到。'));
  sec('board.sector', '類股 / 概念股排行', '<p>看各產業、細產業、概念股、集團股的漲跌與資金流向。</p>' +
    steps(['分類：產業（證交所官方分類）、細產業（如 PCB、被動元件）、概念股（如 AI、低軌衛星、機器人）、集團股（如台塑、鴻海）。',
      '期間：今日、5、10、20、60 日。', '排序：市值加權漲跌、等權平均漲跌、資金流入、法人買超、成交金額。',
      '概念很多時，用右邊的搜尋框找名稱。', '點名稱 → 列出該類股／概念的成分股（依漲跌幅排序）；點領漲股 → 直接看技術圖。']) +
    '<p><b>看資金流向</b>：「成交比重」是這個族群成交金額佔全市場的百分比，後面的 ▲▼ 是比重的變化（今日比前 5 日平均；多日比前一段同長度期間）。比重上升 = 資金流入；搭配「法人買賣超」可以看出是不是法人在買。</p>' +
    note('細產業、概念股、集團股分類來自 Yahoo 股市，每週更新；一檔股票可能同時屬於多個概念。盤中依即時報價計算；法人金額盤中為最近一個已公布的交易日。'));
  sec('board.rank', '法人排行', '<p>三大法人買賣超個股排行。</p>' +
    steps(['選法人：三大法人、外資、投信、自營商。', '選買超或賣超、期間（1～20 日）、排序依張數或金額。', '可只看上市或上櫃、是否含 ETF。', '點股票看技術圖，並可依排行順序逐檔瀏覽。']) +
    '<p><b>欄位</b>：金額 = 每日買賣超張數 × 當日收盤價加總；佔成交量 = 期間買賣超 ÷ 期間成交量；連續 = 到最新一日為止連續買（賣）超天數。</p>');
  sec('board.after', '盤後選股', '<p>法人買賣超在每天收盤後公布，所以這些條件用收盤資料計算。頂端可切換兩種模式：</p>' +
    '<p><b>① 持股均線交叉</b>：法人持股線向上穿越（金叉，BUY）或向下跌破（死叉，SELL）它的 N 日均線。</p>' +
    steps(['勾選條件（可複選，多個條件之間是「或」）。', '設定持股均線天數（3、5 或自訂）、訊號容許天數（1 = 當天剛交叉；3 = 最近 3 天內交叉且目前仍維持）。',
      '按「開始選股」。', '「歷史回測」會把條件套到過去每一天，看訊號後 5／10／20 日的報酬與勝率，並比較不同均線天數。']) +
    '<p><b>② 持股比例變化</b>：最近 N 天，法人持股比例增加（BUY）或減少（SELL）至少 X%。</p>' +
    steps(['勾選條件，設定天數（5、10、20 或自訂）與門檻（1、2、3% 或自訂，可輸入小數）。', '按「開始選股」，結果依比例變化排序。',
      '「參數最佳化」會測試多組天數 × 門檻，列出歷史表現最好的組合；按「套用」直接用那組參數選股。']) +
    note('持股比例變化 = N 天買賣超合計 ÷ 發行張數。例如發行 10 萬張、5 天買超 2,000 張 = +2%。<br>' +
      '最佳化用前 70% 期間挑參數、後 30% 驗證：請以「測試期」的表現為準，標示「測試期失效」的參數只是剛好符合過去。<br>' +
      '「超額報酬」= 訊號股報酬 − 同一天全市場平均，排除大盤漲跌的影響。<br>其他：最低量（張）可排除冷門股；資料日期可選過去某一天，看當時會選出哪些股票。'));
  sec('board.rt', '即時選股', '<p>盤中用即時報價計算當天的指標；收盤後改用收盤資料。</p>' +
    steps(['勾選條件（多個條件之間是「或」）：',
      '・KDJ 買進：K 向上穿越 D，且 K &lt; 20，且今日成交量 &gt; 昨日成交量。',
      '・KDJ 賣出：K 向下穿越 D，且 K &gt; 80，且今日成交量 &gt; 昨日成交量。',
      '・均線糾結突破：MA20 上揚（今天 &gt; 昨天），且 MA5、MA10、MA20 彼此相差在「均線糾結 ≤ X%」以內，且股價由下往上穿越 MA5，且今日成交量 &gt; 昨日成交量。',
      '調整參數：K 低檔／高檔、均線糾結範圍（%）、KD 週期。', '按「開始選股」；開啟「盤中每 60 秒重跑」會自動更新結果。']) +
    note('盤中的「今日成交量」是到目前為止的量，和昨天整天的量比較。結果表的「均線差」= 三條均線最高與最低相差的百分比。'));
  sec('board.line', 'LINE 即時推播', '<p>不用開著網頁，盤中符合即時選股條件時自動傳 LINE 給你。</p>' +
    steps(['到 LINE Developers 建立 Messaging API 頻道，取得 Channel access token 與你的 User ID（U 開頭）。',
      '在「即時選股」頁下方填入兩個欄位，按「儲存 LINE」，再按「傳送測試」確認收到訊息。',
      '選推播範圍（全部自選股／某個群組／全部上市櫃），打開「啟用推播」，按「儲存設定」。推播使用上方勾選的條件與參數。']) +
    note('同一檔、同一條件每天只推一次。LINE 設定屬於你的帳號，其他帳號看不到。'));

  // ── 技術分析圖 ──────────────────────────────────────────
  sec('chart.basic', '看圖與切換股票', steps(['滑鼠滾輪、鍵盤 <kbd>↑</kbd><kbd>↓</kbd> 或 <kbd>PgUp</kbd><kbd>PgDn</kbd>：切換上一檔／下一檔（依你點進來的清單順序）。',
      '工具列「滾輪」切到「縮放」時，滾輪改為縮放 K 線。', '拖曳圖表左右平移；<kbd>+</kbd><kbd>-</kbd> 縮放；<kbd>Esc</kbd> 回清單。',
      '右側清單可直接點選股票；左上 ☆ 加入／移除自選股。', '滑鼠移到圖上，每個區塊左上角會顯示該根 K 棒的數值。']));
  sec('chart.tools', '週期、均線與副圖', steps(['週期：1 分、15 分、60 分、日、週、月。', '均線：MA5～MA240 個別開關；布林通道開關。',
      '副圖：成交量、KDJ、RSI、MACD、投信／外資／自營商／法人持股，點一下開或關。',
      '持股均線：法人持股線的均線天數（盤後選股用的就是這條）。', '持股單位：「張」或「%」（佔發行股數的比例）。',
      '選股訊號：在日 K 上標出法人持股線穿越持股均線的位置（B = 向上、S = 向下）。']) +
    note('外資持股是官方公布的實際張數；投信、自營商沒有公開持股，是用每日買賣超累加推估，所以標示「(估)」。持股「變化」三者都是準確的。'));
  sec('chart.index', '大盤指數圖', '<p>加權指數與櫃買指數也能看技術圖：成交量改為成交金額（億），法人副圖改為三大法人在全市場的累計買賣超（億）。櫃買指數沒有公開的盤中歷史，只能看日線以上。</p>');

  // ── 其他頁面 ─────────────────────────────────────────────
  sec('tw.main', '台股分析', steps(['上方輸入股票代碼（如 2330、0050）按「查詢」，或點快速連結。',
      '頁面依序提供：投資結論、K 線技術分析（含法人持股）、報酬率、財務健康、法人籌碼、技術指標、關鍵價位、布林通道、量能、營收與財務趨勢、情境推演、風險、投資策略、三大法人、融資融券、大中小單、券商分點、同業比較。',
      'K 線技術分析區塊與「台股看盤」的技術圖相同，可切換週期與指標，「在看盤中開啟」可全畫面瀏覽。']));
  sec('tw.monitor', '智慧盯盤', steps(['在「智慧盯盤設定」把操作模式切到「智慧自動」，伺服器每 5 分鐘掃描一次這檔股票。',
      '選策略性格：激進爆發型（5 分 K、突破追價）或穩健保守型。', '填入均價與股數可計算損益並納入判斷。',
      '填入 LINE 金鑰後，出現買進訊號會推播（30 分鐘內不重複）。']) + note('盯盤清單屬於你的帳號，其他帳號看不到。'));
  sec('us.main', '美股分析', steps(['上方輸入美股代碼（如 AAPL、NVDA）按「查詢」，或點快速連結。',
      '「監控」按鈕打開智能即時監控：輸入代碼（可填持股數與均價）加入，最多 10 支，每 60 秒更新。',
      '頁面內容包含投資結論、TradingView 圖表、財務、法人籌碼、技術指標、風險與策略建議、相關新聞。']));
  sec('screener.main', '策略選股', steps(['選市場（台股／美股）與掃描標的（產業分類或自訂代碼）。', '選歷史資料週期。',
      '加入篩選條件（均線、KD、MACD…），每個條件可調參數；所有條件都符合才會列出。', '按「開始掃描」。',
      '可把條件存成策略，之後一鍵載入；也可對結果設定出場提醒。']) + note('儲存的策略屬於你的帳號。'));
  sec('portfolio.main', '投資組合', steps(['「自選群組」：新增群組並加入股票，整理想追蹤的標的。', '「持倉損益」：輸入持股、成本、日期，計算未實現損益與各標的損益率。',
      '「多股比較」：選幾檔股票比較報酬走勢。']) + note('投資組合存在你的帳號，換裝置登入也看得到；其他帳號看不到。'));
  sec('account.main', '帳號設定', steps(['輸入目前密碼與兩次新密碼（至少 8 碼，需含英文與數字），按「更新密碼」。', '更新後，其他裝置上的登入會自動登出。']));
  sec('admin.main', '使用者管理（管理員）', steps(['新增帳號：輸入帳號、初始密碼、權限。', '重設密碼、切換管理員／使用者權限、刪除帳號。',
      '重設密碼或調整權限後，該帳號需重新登入。刪除帳號會一併刪除該帳號的個人資料。']));

  var PAGES = {
    board: ['board.index', 'board.pulse', 'board.all', 'board.watch', 'board.sector', 'board.rank', 'board.after', 'board.rt', 'board.line'],
    chart: ['chart.basic', 'chart.tools', 'chart.index'],
    tw: ['tw.main', 'tw.monitor', 'chart.tools'], us: ['us.main'], screener: ['screener.main'],
    portfolio: ['portfolio.main'], account: ['account.main'], admin: ['admin.main'],
  };

  var css = '.hp-mask{position:fixed;inset:0;background:var(--scrim);z-index:900;opacity:0;transition:opacity .2s}' +
    '.hp-mask.on{opacity:1}' +
    '.hp-panel{position:fixed;top:0;right:0;bottom:0;width:min(460px,100vw);z-index:901;background:var(--card-bg);border-left:1px solid var(--card-bd2);' +
    'box-shadow:-20px 0 50px rgba(0,0,0,.25);display:flex;flex-direction:column;transform:translateX(100%);transition:transform .25s cubic-bezier(.2,.8,.2,1)}' +
    '.hp-panel.on{transform:none}' +
    '.hp-head{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--line)}' +
    '.hp-head b{font-size:1.05rem}.hp-x{border:none;background:var(--overlay-1);color:var(--text);width:32px;height:32px;border-radius:9px;cursor:pointer;font-size:1rem}' +
    '.hp-toc{display:flex;flex-wrap:wrap;gap:6px;padding:12px 20px;border-bottom:1px solid var(--line)}' +
    '.hp-toc a{font-size:.76rem;padding:4px 10px;border-radius:999px;background:var(--overlay-1);border:1px solid var(--line);color:var(--text-mid);cursor:pointer;text-decoration:none}' +
    '.hp-toc a:hover,.hp-toc a.on{color:var(--accent);border-color:var(--accent)}' +
    '.hp-body{flex:1;overflow-y:auto;padding:6px 20px 30px;font-size:.88rem;line-height:1.75;color:var(--text)}' +
    '.hp-sec{padding:14px 0;border-bottom:1px dashed var(--line);scroll-margin-top:8px}.hp-sec.flash{animation:hpf 1.4s}' +
    '@keyframes hpf{0%{background:rgba(109,93,252,.15)}100%{background:transparent}}' +
    '.hp-sec h4{font-size:1rem;margin-bottom:6px}.hp-sec ol{padding-left:20px;margin:6px 0}.hp-sec li{margin:3px 0}' +
    '.hp-note{margin-top:8px;padding:9px 12px;border-radius:10px;background:var(--overlay-1);border:1px solid var(--line);color:var(--text-mid);font-size:.8rem}' +
    '.hp-sec kbd{font-family:"JetBrains Mono",monospace;font-size:.72rem;border:1px solid var(--card-bd2);border-radius:5px;padding:0 5px}' +
    '.help-btn{display:inline-flex;align-items:center;gap:5px;height:28px;padding:0 11px;border-radius:999px;border:1px solid var(--card-bd2);' +
    'background:var(--surface-1);color:var(--text-mid);font-size:.76rem;font-weight:600;cursor:pointer;white-space:nowrap}' +
    '.help-btn:hover{color:var(--accent);border-color:var(--accent)}';

  var mask, panel;
  function build() {
    if (panel) return;
    var st = document.createElement('style'); st.textContent = css; document.head.appendChild(st);
    mask = document.createElement('div'); mask.className = 'hp-mask'; mask.onclick = close;
    panel = document.createElement('aside'); panel.className = 'hp-panel'; panel.setAttribute('aria-label', '操作說明');
    document.body.appendChild(mask); document.body.appendChild(panel);
  }
  function close() {
    if (!panel) return;
    panel.classList.remove('on'); mask.classList.remove('on');
    setTimeout(function () { mask.style.display = 'none'; }, 220);
  }
  function open(id) {
    build();
    var page = document.body.getAttribute('data-help-page') || (id || '').split('.')[0];
    var ids = (PAGES[page] || []).slice();
    if (id && H[id] && ids.indexOf(id) < 0) ids.unshift(id);
    if (!ids.length) ids = Object.keys(H);
    panel.innerHTML = '<div class="hp-head"><b>操作說明</b><button class="hp-x" aria-label="關閉">✕</button></div>' +
      '<div class="hp-toc">' + ids.map(function (k) { return '<a data-to="' + k + '">' + H[k].t + '</a>'; }).join('') + '</div>' +
      '<div class="hp-body">' + ids.map(function (k) {
        return '<section class="hp-sec" id="hp-' + k.replace('.', '-') + '"><h4>' + H[k].t + '</h4>' + H[k].b + '</section>'; }).join('') + '</div>';
    panel.querySelector('.hp-x').onclick = close;
    panel.querySelector('.hp-toc').onclick = function (e) { var a = e.target.closest('[data-to]'); if (a) go(a.getAttribute('data-to')); };
    mask.style.display = 'block';
    requestAnimationFrame(function () { mask.classList.add('on'); panel.classList.add('on'); if (id && H[id]) go(id); });
  }
  function go(id) {
    var el = document.getElementById('hp-' + id.replace('.', '-'));
    if (!el) return;
    el.scrollIntoView({block: 'start', behavior: 'smooth'});
    el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
    panel.querySelectorAll('.hp-toc a').forEach(function (a) { a.classList.toggle('on', a.getAttribute('data-to') === id); });
  }
  document.addEventListener('click', function (e) {
    var t = e.target.closest && e.target.closest('[data-help]');
    if (!t) return;
    e.preventDefault(); e.stopPropagation();
    var v = t.getAttribute('data-help');
    open(v === 'page' ? null : v);
  }, true);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && panel && panel.classList.contains('on')) { e.stopPropagation(); close(); } }, true);
  window.Help = {open: open, close: close};
})();
