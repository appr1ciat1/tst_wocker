# -*- coding: utf-8 -*-
"""法人籌碼互動 widget（供四策略報表 + paper trading 共用）。

自包含 HTML/CSS/JS 區塊，在瀏覽器端即時抓取資料源 GitHub Pages 的排名檔，
與資料源網頁 https://appr1ciat1.github.io/tw-institutional-stocker/ 完全同口徑：

- 類別：外資 / 投信 / 自營商 / 主力(券商分點) / 三大法人合計
- 時間段：3 / 5 / 10 / 20 日
- 方向：買超 / 賣超
- 前 30 名、可排除 ETF
- 排序一律為「N 日累計買賣超張數」（官方每日數據加總，可與券商軟體對帳）

CSS class 全部以 `lchip-` 前綴命名，避免污染宿主報表樣式。
"""

# 資料源（GitHub Pages，回傳 application/json + CORS *；raw 亦可）
INST_DATA_BASE = "https://appr1ciat1.github.io/tw-institutional-stocker/data"


def build_inst_widget(base_url: str = INST_DATA_BASE) -> str:
    """回傳自包含的法人籌碼 widget HTML 字串。

    注意：不得包含 'backtest_chart.png' 或 'AI 台股量化交易 vX.X' 等字串，
    以免被 update_ai_report.yml 的 sed 取代破壞。
    """
    return r"""
<section class="lchip-widget" id="lchipWidget">
  <h2>🏛️ 法人籌碼排行（外資／投信／自營商／主力／三大法人合計）</h2>
  <p class="lchip-note">
    口徑同券商 App：一律依「N 日累計買賣超張數」排序，官方每日數據加總、可逐檔對帳。
    資料源：<a href="https://appr1ciat1.github.io/tw-institutional-stocker/" target="_blank" rel="noopener">tw-institutional-stocker</a>
    <span id="lchipAsof"></span>
  </p>
  <div class="lchip-controls">
    <label>類別
      <select id="lchipCat">
        <option value="foreign">外資</option>
        <option value="trust">投信</option>
        <option value="dealer">自營商</option>
        <option value="main_force">主力（券商分點）</option>
        <option value="three_inst_net">三大法人合計</option>
      </select>
    </label>
    <label>時間段
      <select id="lchipWin">
        <option value="3">3 日</option>
        <option value="5" selected>5 日</option>
        <option value="10">10 日</option>
        <option value="20">20 日</option>
      </select>
    </label>
    <label>方向
      <select id="lchipDir">
        <option value="up">買超</option>
        <option value="down">賣超</option>
      </select>
    </label>
    <label class="lchip-check"><input type="checkbox" id="lchipEtf" checked> 排除 ETF</label>
  </div>
  <div class="lchip-tablewrap">
    <table class="lchip-table" id="lchipTable">
      <thead><tr id="lchipHead"></tr></thead>
      <tbody id="lchipBody"><tr><td>載入中…</td></tr></tbody>
    </table>
  </div>
</section>

<style>
.lchip-widget{margin:22px 0;padding:18px;border-radius:12px;background:rgba(255,255,255,.03);
  border:1px solid rgba(255,255,255,.08);}
.lchip-widget h2{margin:0 0 6px;font-size:1.15rem;}
.lchip-note{margin:0 0 12px;font-size:.82rem;color:#9aa;line-height:1.5;}
.lchip-note a{color:#4FC3F7;}
.lchip-controls{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;margin-bottom:12px;}
.lchip-controls label{font-size:.85rem;color:#cbd;display:inline-flex;align-items:center;gap:6px;}
.lchip-controls select{background:#1b1b28;color:#eee;border:1px solid #3a3a52;border-radius:6px;
  padding:5px 8px;font-size:.85rem;}
.lchip-check{cursor:pointer;}
.lchip-tablewrap{overflow-x:auto;}
.lchip-table{width:100%;border-collapse:collapse;font-size:.84rem;}
.lchip-table th,.lchip-table td{padding:6px 9px;text-align:right;white-space:nowrap;
  border-bottom:1px solid rgba(255,255,255,.06);}
.lchip-table th:nth-child(2),.lchip-table td:nth-child(2){text-align:left;}
.lchip-table th{color:#8b8b9e;font-weight:600;position:sticky;top:0;background:#15151f;}
.lchip-table tbody tr:hover{background:rgba(255,255,255,.04);}
.lchip-badge{display:inline-block;background:#2a2a40;border-radius:4px;padding:1px 6px;
  margin-right:6px;font-family:monospace;font-size:.8rem;color:#9cf;}
.lchip-pos{color:#26de81;font-weight:600;}
.lchip-neg{color:#ff6b6b;font-weight:600;}
</style>

<script>
(function(){
  var BASE = "__BASE__";
  var TOP_N = 30;
  var cat = "foreign", win = "5", dir = "up", excludeEtf = true;
  var reqSeq = 0;  // 單調遞增請求序號：只套用最新一次 render 的回應，避免快速切換時舊 fetch 覆蓋

  function $(id){ return document.getElementById(id); }
  function fmt(n){ n = Number(n)||0; return n.toLocaleString(); }
  function lots(n){ n = Number(n)||0;
    return '<span class="'+(n>=0?'lchip-pos':'lchip-neg')+'">'+(n>=0?'+':'')+fmt(n)+'</span>'; }
  function pct(n){ return (Number(n)||0).toFixed(2); }
  function isEtf(code){ code = String(code||""); return !(code.length===4 && /^[0-9]{4}$/.test(code)); }

  // 各類別：資料檔 + 欄位定義（順序即顯示順序，第2欄固定為「股票」）
  var COLS = {
    foreign:        [["買賣超(張)", function(r){return lots(r.net_lots);}],
                     ["外資持股%", function(r){return pct(r.ratio)+"%";}]],
    trust:          [["買賣超(張)", function(r){return lots(r.net_lots);}],
                     ["佔股本%", function(r){return pct(r.pct_cap)+"%";}]],
    dealer:         [["買賣超(張)", function(r){return lots(r.net_lots);}],
                     ["佔股本%", function(r){return pct(r.pct_cap)+"%";}]],
    main_force:     [["主力買賣超(張)", function(r){return lots(r.net_lots);}],
                     ["分點數", function(r){return String(r.n_brokers||0);}]],
    three_inst_net: [["合計(張)", function(r){return lots(r.net_lots);}],
                     ["外資", function(r){return lots(r.foreign_lots);}],
                     ["投信", function(r){return lots(r.trust_lots);}],
                     ["自營", function(r){return lots(r.dealer_lots);}],
                     ["佔股本%", function(r){return pct(r.pct_cap)+"%";}]]
  };

  function fetchJson(url){
    return fetch(url).then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); });
  }

  function loadRows(){
    if(cat === "main_force"){
      return fetchJson(BASE + "/main_force_rankings.json").then(function(d){
        var arr = (d.windows && d.windows[win]) ? d.windows[win].slice() : [];
        if(dir === "down"){ arr.reverse(); }
        arr = arr.filter(function(r){ return dir==="up" ? r.net_lots>0 : r.net_lots<0; });
        return { rows: arr, date: d.date };
      });
    }
    var file = (cat === "three_inst_net")
      ? "top_three_inst_net_" + win + "_" + dir + ".json"
      : "top_" + cat + "_change_" + win + "_" + dir + ".json";
    return fetchJson(BASE + "/" + file).then(function(arr){
      return { rows: arr, date: (arr.length ? arr[0].date : "") };
    });
  }

  function render(){
    var head = $("lchipHead"), body = $("lchipBody");
    var cols = COLS[cat];
    var myReq = ++reqSeq;  // 本次請求序號
    head.innerHTML = "<th>#</th><th>股票</th><th>市場</th>" +
      cols.map(function(c){ return "<th>"+c[0]+"</th>"; }).join("");
    body.innerHTML = "<tr><td colspan='"+(3+cols.length)+"'>載入中…</td></tr>";

    loadRows().then(function(res){
      if(myReq !== reqSeq){ return; }  // 已被更新的請求取代，丟棄舊回應
      var rows = res.rows.filter(function(r){ return !(excludeEtf && isEtf(r.code)); });
      $("lchipAsof").textContent = res.date ? "｜資料截至 " + res.date : "";
      if(!rows.length){
        body.innerHTML = "<tr><td colspan='"+(3+cols.length)+"'>此條件下無資料</td></tr>";
        return;
      }
      body.innerHTML = rows.slice(0, TOP_N).map(function(r, i){
        var cells = cols.map(function(c){ return "<td>"+c[1](r)+"</td>"; }).join("");
        return "<tr><td>"+(i+1)+"</td><td><span class='lchip-badge'>"+r.code+"</span>"+
          (r.name||"")+"</td><td>"+(r.market||"")+"</td>"+cells+"</tr>";
      }).join("");
    }).catch(function(e){
      if(myReq !== reqSeq){ return; }
      body.innerHTML = "<tr><td colspan='"+(3+cols.length)+"'>載入失敗："+e.message+"</td></tr>";
    });
  }

  function init(){
    $("lchipCat").addEventListener("change", function(){ cat=this.value; render(); });
    $("lchipWin").addEventListener("change", function(){ win=this.value; render(); });
    $("lchipDir").addEventListener("change", function(){ dir=this.value; render(); });
    $("lchipEtf").addEventListener("change", function(){ excludeEtf=this.checked; render(); });
    render();
  }
  if(document.readyState === "loading"){ document.addEventListener("DOMContentLoaded", init); }
  else { init(); }
})();
</script>
""".replace("__BASE__", base_url)
