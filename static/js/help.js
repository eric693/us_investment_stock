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
    '<p><b>③ 外資投信同買</b>：最近 N 天外資與投信持股比例都增加至少 X%，且收盤站上 M 日均線（預設 5 天、0.3%、MA20）。</p>' +
    note('持股比例變化 = N 天買賣超合計 ÷ 發行張數。例如發行 10 萬張、5 天買超 2,000 張 = +2%。<br>' +
      '最佳化用前 70% 期間挑參數、後 30% 驗證：請以「測試期」的表現為準，標示「測試期失效」的參數只是剛好符合過去。<br>' +
      '「超額報酬」= 訊號股報酬 − 同一天全市場平均，排除大盤漲跌的影響。<br>其他：最低量（張）可排除冷門股；資料日期可選過去某一天，看當時會選出哪些股票。'));
  sec('board.score', '策略成績單', '<p>把系統所有選股條件套到過去約兩年的資料上，看每種條件出現後的表現，幫你挑出比較可靠的方法。</p>' +
    steps(['選「看之後 5／10／20 日」：訊號出現後持有幾天計算報酬。', '「只看穩定」只列出前後兩段期間都贏大盤的策略。',
      '點一列展開，看今天符合這個策略的股票；點股票進技術圖，並可逐檔瀏覽。']) +
    '<p><b>怎麼看</b>：超額報酬 = 訊號股報酬 − 同一天全市場平均；勝率 = 之後上漲的比例（賣出訊號為下跌比例）。資料切成前段與後段，兩段都贏大盤才標「穩定」，排序依較差那一段，越上面越可靠。</p>' +
    note('只計入 20 日均量 ≥ 500 張的股票；訊號只算條件剛成立那天；報酬未還原除權息。過去的表現不代表未來，請搭配停損與分散持股。每天收盤資料更新後自動重算。'));
  sec('board.rt', '即時選股', '<p>盤中用即時報價計算當天的指標；收盤後改用收盤資料。</p>' +
    steps(['勾選條件（多個條件之間是「或」）：',
      '・KDJ 買進：K 向上穿越 D，且 K &lt; 20，且今日成交量 &gt; 昨日成交量。',
      '・KDJ 賣出：K 向下穿越 D，且 K &gt; 80，且今日成交量 &gt; 昨日成交量。',
      '・均線糾結突破：MA20 上揚（今天 &gt; 昨天），且 MA5、MA10、MA20 彼此相差在「均線糾結 ≤ X%」以內，且股價由下往上穿越 MA5，且今日成交量 &gt; 昨日成交量。',
      '調整參數：K 低檔／高檔、均線糾結範圍（%）、KD 週期。', '按「開始選股」；開啟「盤中每 60 秒重跑」會自動更新結果。']) +
    note('盤中的「今日成交量」是到目前為止的量，和昨天整天的量比較。結果表的「均線差」= 三條均線最高與最低相差的百分比。'));
  sec('board.line', 'LINE 即時推播', '<p>不用開著網頁，盤中符合即時選股條件時自動通知你。可以用 LINE，也可以用手機／瀏覽器通知（不佔 LINE 額度），兩個都開就兩邊都收到。</p>' +
    steps(['到「帳號設定 → 通知管道」照三個步驟設定 LINE：貼上 Channel access token 與 Channel secret、設定 Webhook、傳綁定碼給官方帳號（系統自動取得你的 User ID）。',
      '回到這裡按「傳送測試」確認收到。',
      '選推播範圍（全部自選股／某個群組／全部上市櫃），打開「啟用推播」，按「儲存設定」。推播使用上方勾選的條件與參數。']) +
    note('同一輪的訊號會合併成一則；同一檔、同一條件每天只推一次。LINE 輕用量方案每月 200 則，到上限就只送手機通知（帳號設定可看用量），範圍建議選自選股。推播失敗時，導覽列的鈴鐺會亮紅點，點進去可看原因。'));

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
  sec('picks.main', '潛力股評分', '<p>用多個經過歷史驗證的指標替每檔股票打 0～100 分，分數越高代表「過去同類型的股票之後平均表現越好」。台股與美股各用一套指標，右上角切換。</p>' +
    steps(['<b>上方四個數字</b>：分數前 20 名過去的平均表現、最高分組與最低分組的差距、綜合分數的 IC（預測力），以及單檔上漲機率。',
      '<b>回測證據</b>：左圖是「每 N 天換一次、持有前 20 名」扣掉交易成本後和全體平均的累積報酬；右圖把股票依分數分成 10 組，越往右越高代表分數有區分力。切換 5／10／20 日看不同持有期間。',
      '<b>使用的指標</b>：每個指標在前段、後段的 IC。指標與方向只用前段決定，後段是驗證；兩段都為正才標「穩定」。',
      '<b>今日潛力股</b>：依分數排序。每檔列出各指標的百分位、分析理由（紫色）與風險提示（黃色），以及同一分組過去 20 日的平均超額報酬、上漲機率和停損參考價。',
      '搜尋框可查任何一檔的分數；查不到通常是成交量太小或上市不滿半年。點名稱看技術圖，「完整分析」進個股分析頁。']) +
    '<p><b>台股指標</b>：外資買超、突破強度、月線位置、營收成長、投信不擁擠（投信大買反而扣分）。<b>美股指標</b>：12 個月動能、長期趨勢、強勢回檔。</p>' +
    note('分數是統計上的機率優勢，不是保證。前 20 名的平均表現贏大盤，但單一檔贏大盤的機率約一半，獲利來自少數大漲股，所以要分散持有。資料每天收盤後自動更新。'));
  sec('screener.main', '策略選股', steps(['選市場（台股／美股）與掃描標的（產業分類或自訂代碼）。', '選歷史資料週期。',
      '加入篩選條件（均線、KD、MACD…），每個條件可調參數；所有條件都符合才會列出。', '按「開始掃描」。',
      '可把條件存成策略，之後一鍵載入；也可對結果設定出場提醒。']) + note('儲存的策略屬於你的帳號。'));
  sec('portfolio.main', '投資組合', steps(['「自選群組」：新增群組並加入股票，整理想追蹤的標的。', '「持倉損益」：輸入持股、成本、日期，計算未實現損益與各標的損益率。',
      '「多股比較」：選幾檔股票比較報酬走勢。']) + note('投資組合存在你的帳號，換裝置登入也看得到；其他帳號看不到。'));
  sec('account.main', '帳號設定', steps(['輸入目前密碼與兩次新密碼（至少 8 碼，需含英文與數字），按「更新密碼」。', '更新後，其他裝置上的登入會自動登出。']) +
    '<p><b>通知設定</b>：即時選股、智慧盯盤、策略選股出場提醒的通知都送到這裡設定的管道。</p>' +
    steps(['手機／瀏覽器通知：在要收通知的裝置上按「在這台裝置開啟通知」並允許。iPhone 要先用 Safari「加入主畫面」，再從主畫面打開本站開啟（iOS 16.4 以上）。',
      'LINE 推播：① 貼上 Channel access token 與 Channel secret，按「驗證並儲存」② 把 Webhook URL 貼到 LINE Developers ③ 產生綁定碼並用 LINE 傳給官方帳號。', '按「傳送測試通知」確認收得到。']) +
    '<p><b>每日潛力股</b>：選台股／美股、發送時間、每個交易日或每週一次、前幾名，按「儲存設定」；「現在送一次看看」可立即收一則。</p>' +
    note('推播失敗（例如 LINE token 過期、額度用完）時，導覽列的鈴鐺會亮紅點，這裡會顯示原因。LINE 每月額度卡片顯示本月用量，可自訂上限。'));
  sec('admin.main', '使用者管理（管理員）', steps(['新增帳號：輸入帳號、初始密碼、權限。', '重設密碼、切換管理員／使用者權限、刪除帳號。',
      '重設密碼或調整權限後，該帳號需重新登入。刪除帳號會一併刪除該帳號的個人資料。']));

  sec('commodity.main', '黃金・原油進出場', '<p>用固定規則在黃金、原油的 1 小時、4 小時、日線圖上標出進出場點，並用過去資料回測成績。</p>' +
    steps(['上方切換商品（黃金／原油）與週期（1 小時／4 小時／日線）。',
      '圖上紅色 ▲「進」是進場，▼「出」是出場並標示報酬；紅色虛線階梯是每筆交易的出場線（移動停損）。',
      '持有中時，圖上會畫三條橫線：出場線（跌破就出場）、進場價、參考目標 2R。',
      '右側「歷史回測」列出全部、前半段、後半段的成績；兩段都賺錢的週期較可靠。點交易紀錄的一列可以跳到圖上位置。']) +
    note('規則只做多，參數沒有針對過去資料最佳化。目前回測以黃金 4 小時、日線最穩定；黃金 1 小時、原油日線前後段差異大。回測不代表未來，僅供參考。'));

  sec('financials.main', '財報分析', '<p>用基本面經理人的流程分析一檔台股或美股，最後給出「買進／逢低布局／持有觀望／避開」的建議與合理價。</p>' +
    steps(['輸入台股代號（2330）或美股代碼（AAPL），按「分析」。',
      '最上方是結論：建議、綜合分數（公司品質 + 估值吸引力）、合理價與買進價參考（合理價打 85 折）。',
      '「投資論點」列出亮點、隱憂與財務警訊；有警訊時建議會直接降級。',
      '「五大面向評分」：成長性 25%、獲利能力 20%、財務體質 15%、盈餘品質 15%、估值 25%，點選可看每個指標的數值與意義。',
      '合理價取三種方法：本益比法（未來 12 個月 EPS × 合理本益比）、兩段式現金流折現、分析師目標價。']) +
    note('台股另看月營收與同產業本益比、殖利率中位數；金融股改用 ROE 與股價淨值比估值。規則化分析，僅供參考，非投資建議。'));

  sec('etf.main', '台股 ETF 分析', '<p>一次比較最多 5 檔台股 ETF：價格是否合理、配息與填息、成分股重不重疊。</p>' +
    steps(['上方加入代號（或點熱門、右下的即將除息清單），按「比較」。',
      '「折溢價」超過 ±1% 代表市價偏離淨值，溢價時買進等於買貴；「今日資金流向」看申購贖回。',
      '「近 12 個月殖利率」用實際配過的錢計算；「填息」是除息後漲回除息前價格要幾個交易日。',
      '「成分股重疊度」50% 以上代表兩檔有一半買一樣的股票；「合併後實際持有的股票」可在每檔旁填比重，看整體押在哪些股票。',
      '「配息月份」可以挑不同月份配息的 ETF 組合，讓每月都有現金流。']) +
    note('淨值與折溢價來自證交所即時淨值，盤中約每分鐘更新；成分股每日更新。'));

  var PAGES = {
    board: ['board.index', 'board.pulse', 'board.all', 'board.watch', 'board.score', 'board.sector', 'board.rank', 'board.after', 'board.rt', 'board.line'],
    chart: ['chart.basic', 'chart.tools', 'chart.index'],
    tw: ['tw.main', 'tw.monitor', 'chart.tools'], us: ['us.main'], screener: ['screener.main'], picks: ['picks.main'], commodity: ['commodity.main'], financials: ['financials.main'], etf: ['etf.main'],
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
