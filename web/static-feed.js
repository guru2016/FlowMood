/* FlowMood 静态模式数据层（GitHub Pages 专用）
 *
 * 本地/VPS 看板由 dashboard_server.py 提供 /api/*；这里用同一套接口形状，
 * 让 web/dashboard.html 在没有任何后端的静态托管上也能跑：
 *   - 报告 / 状态 / 排名 来自 build_static.py 内嵌的 FLOWMOOD_BOOT 快照
 *   - 价格、涨跌、资金流由浏览器直连行情源每 20 秒刷新（东财 ulist.np 为主，
 *     同花顺 realhead 逐只兜底，两者都是公开只读接口）
 *   - 评分沿用快照值，页面明确标注，不伪造"实时重算"
 */
(function () {
  'use strict';
  var BOOT = window.FLOWMOOD_BOOT || {};
  var RANK0 = (BOOT.ranking && BOOT.ranking.ranking) || [];
  var REFRESH_MS = 20000;

  // 报告候选里的标的不一定在 Top100 榜单里，也要一起拿行情，
  // 否则从报告正文点开个股详情时价格 / 涨跌 / 资金流全是空的。
  var EXTRA = [];
  Object.keys(BOOT.reports || {}).forEach(function (k) {
    ((BOOT.reports[k] || {}).plans || []).forEach(function (p) { if (p.symbol) EXTRA.push(p.symbol); });
  });
  ((BOOT.status && BOOT.status.report && BOOT.status.report.plans) || []).forEach(function (p) {
    if (p.symbol) EXTRA.push(p.symbol);
  });
  EXTRA = EXTRA.filter(function (s, i, a) {
    return a.indexOf(s) === i && RANK0.every(function (r) { return r.symbol !== s; });
  });

  var FEED = {
    rows: RANK0.map(function (r) { return Object.assign({}, r); }),
    quotes: {}, extraQuotes: {}, live: false, reason: '正在连接行情源…', at: '', source: 'snapshot',
    hit: 0, total: RANK0.length, warning: '', digest: [], started: false, failStreak: 0
  };

  function codeOf(symbol) { return String(symbol || '').split('.')[0]; }
  function secid(symbol) {
    var c = codeOf(symbol), s = String(symbol || '');
    if (s.indexOf('.SH') >= 0 || /^(5|6|9)/.test(c)) return '1.' + c;
    return '0.' + c;                       // 深市 / 创业板 / 北交所
  }
  function num(v) { var n = Number(v); return isFinite(n) ? n : null; }
  // 统一按北京时间输出 ISO，前端 slice(11,19) 才是 09:35:12 而不是 01:35:12
  function bjIso() {
    var parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
    }).formatToParts(new Date());
    var v = {}; parts.forEach(function (p) { v[p.type] = p.value; });
    return v.year + '-' + v.month + '-' + v.day + 'T' + v.hour + ':' + v.minute + ':' + v.second + '+08:00';
  }

  // ---- JSONP：<script> 不受同源策略限制，回调名可自定义或由上游固定 ----
  function jsonp(url, cbName, timeoutMs) {
    return new Promise(function (resolve) {
      var done = false, s = document.createElement('script'), timer;
      function finish(value) {
        if (done) return;
        done = true; clearTimeout(timer);
        try { delete window[cbName]; } catch (e) { window[cbName] = undefined; }
        if (s.parentNode) s.parentNode.removeChild(s);
        resolve(value);
      }
      window[cbName] = function (data) { finish(data); };
      timer = setTimeout(function () { finish(null); }, timeoutMs || 7000);
      s.src = url;
      s.onerror = function () { finish(null); };
      document.head.appendChild(s);
    });
  }

  // ---- 主源：东财批量报价，一次拿全榜单的价格/涨跌/换手/主力资金流 ----
  function emBatch(symbols) {
    if (!symbols.length) return Promise.resolve(null);
    var cb = 'FM_EM_' + Date.now().toString(36);
    var ids = symbols.map(secid).join(',');
    var url = 'https://push2.eastmoney.com/api/qt/ulist.np/get?cb=' + cb +
      '&fltt=2&invt=2&secids=' + encodeURIComponent(ids) +
      '&fields=f2,f3,f6,f8,f12,f14,f15,f16,f17,f18,f62,f184,f100';
    return jsonp(url, cb, 8000).then(function (res) {
      var diff = res && res.data && res.data.diff;
      if (!diff || !diff.length) return null;
      var out = {};
      diff.forEach(function (r) {
        var c = String(r.f12 || '');
        if (c.length !== 6) return;
        out[c] = {
          f2: num(r.f2), f3: num(r.f3), f6: num(r.f6), f8: num(r.f8),
          f14: r.f14, f184: num(r.f184), f100: r.f100
        };
      });
      return Object.keys(out).length ? out : null;
    });
  }

  // ---- 兜底：同花顺 realhead，回调名由 URL 路径固定，逐只加载 ----
  function thsOne(code) {
    var cb = 'quotebridge_v2_realhead_hs_' + code + '_last';
    var url = 'https://d.10jqka.com.cn/v2/realhead/hs_' + code + '/last.js';
    return jsonp(url, cb, 6500).then(function (res) {
      var it = res && res.items;
      if (!it) return null;
      var n = function (k) { return num(it[k]); };
      var price = n('10'), prev = n('6');
      if (!price || price <= 0 || !prev) return null;
      return { f2: price, f18: prev, f3: n('199112'), f6: n('19'), f8: n('1968584'), f14: it.name || '' };
    });
  }
  function thsBatch(codes, concurrency) {
    var out = {}, i = 0;
    var workers = Array.from({ length: Math.min(concurrency || 16, codes.length) }, function () {
      return (function next() {
        if (i >= codes.length) return Promise.resolve();
        var c = codes[i++];
        return thsOne(c).then(function (q) { if (q) out[c] = q; return next(); });
      })();
    });
    return Promise.all(workers).then(function () { return out; });
  }

  // 按代码列表顺序拉腾讯行情（串行分批，给「只刷榜单 100 只」的降级路径用）
  function txBatch(codes) {
    var groups = [];
    for (var i = 0; i < codes.length; i += TX_BATCH_SIZE) {
      groups.push(codes.slice(i, i + TX_BATCH_SIZE).map(txSymbol));
    }
    var out = {};
    return groups.reduce(function (p, g) {
      return p.then(function () {
        return txGroup(g).then(function (q) {
          if (q) for (var k in q) out[k] = q[k];
        });
      });
    }, Promise.resolve()).then(function () { return out; });
  }

  function applyQuotes() {
    var hit = 0;
    FEED.rows.forEach(function (r) {
      var q = FEED.quotes[codeOf(r.symbol)];
      if (!q) { delete r.quote_source; return; }
      hit++;
      if (q.f2 != null && q.f2 > 0) r.price = q.f2;
      if (q.f3 != null) r.change_pct = q.f3;
      if (q.f8 != null) r.turnover_pct = q.f8;
      if (q.f184 != null) r.money_flow_pct = q.f184;   // 只有东财提供，兜底时保留快照
      r.quote_source = FEED.source;
    });
    FEED.hit = hit;
    FEED.extraQuotes = {};
    EXTRA.forEach(function (sym) {
      var q = FEED.quotes[codeOf(sym)];
      if (!q) return;
      FEED.extraQuotes[sym] = {
        price: q.f2, change_pct: q.f3, money_flow_pct: q.f184,
        turnover_pct: q.f8, sector: q.f100
      };
    });
  }

  function buildDigest() {
    var rows = FEED.rows.slice(0, 100), out = [];
    var valid = function (k) { return rows.filter(function (r) { return r[k] != null; }); };
    var up = valid('change_pct').sort(function (a, b) { return b.change_pct - a.change_pct; }).slice(0, 2);
    var flow = valid('money_flow_pct').sort(function (a, b) { return b.money_flow_pct - a.money_flow_pct; }).slice(0, 2);
    var buys = rows.filter(function (r) { return r.action === '条件买入'; }).length;
    var sectors = {};
    rows.slice(0, 30).forEach(function (r) { var k = r.sector || '未分类'; sectors[k] = (sectors[k] || 0) + 1; });
    var topSec = Object.entries(sectors).sort(function (a, b) { return b[1] - a[1]; })[0] || ['--', 0];
    if (up.length) out.push({ kind: 'up', text: '实时涨幅居前：' + up.map(function (r) {
      return r.name + ' ' + (r.change_pct > 0 ? '+' : '') + Number(r.change_pct).toFixed(2) + '%'; }).join('、') });
    if (flow.length) out.push({ kind: 'flow', text: '主力净流入居前：' + flow.map(function (r) {
      return r.name + ' ' + (r.money_flow_pct > 0 ? '+' : '') + Number(r.money_flow_pct).toFixed(2) + '%'; }).join('、') });
    out.push({ kind: 'buy', text: '榜单内条件买入 ' + buys + ' 只 · Top30 最集中板块 ' + topSec[0] + '（' + topSec[1] + ' 只）' });
    out.push({ kind: 'heart', text: '行情通道 ' + FEED.source + ' · 覆盖 ' + FEED.hit + '/' + FEED.total + ' 只 · ' + (FEED.at || '--') });
    if (UNIVERSE.length) {
      out.push({ kind: 'warn', text: MARKET_SCORED
        ? '静态托管：全市场 ' + UNIVERSE.length + ' 只行情浏览器直连，七维评分在本地浏览器重算（公式与本地看板同源）'
        : '静态托管：盘口已实时，七维评分 ' + (PHASE.key === 'auction' ? '09:30 起重算（竞价无成交额）' : '待行情返回后重算') });
    } else {
      out.push({ kind: 'warn', text: '在线演示版：价格 / 涨跌 / 资金流为浏览器直连实时行情，评分沿用最近一次快照评分' });
    }
    return out;
  }

  // ---- 全市场基座：七维评分里「一天只变一次」的那部分量 ----
  // build_static.py 把 5000+ 只的 atr14 / ma20 / high20 / pivot_price /
  // trend_consistency / avg_amount20 打进页面；盘中会变的只有价格、成交额、
  // 换手、资金流，那些由下面现拉。两者一拼，浏览器就能对全市场重算评分。
  var BASE = BOOT.base || null;
  var SCORE_CFG = BOOT.config || {};
  var UNIVERSE = [];
  var MARKET_STATS = null;
  var MARKET_BREADTH = null;
  var MARKET_SCORED = false;
  var MARKET_AT = '';
  var PHASE = { key: 'closed', label: '—', live: false, scored: false };

  (function buildUniverse() {
    if (!BASE || !Array.isArray(BASE.rows) || !BASE.rows.length) return;
    var keys = BASE.keys;
    var out = [];
    for (var i = 0; i < BASE.rows.length; i++) {
      var line = BASE.rows[i], r = {};
      for (var k = 0; k < keys.length; k++) r[keys[k]] = line[k];
      if (!r.symbol) continue;
      r.code = codeOf(r.symbol);
      r.limit_up = !!r.limit_up;
      out.push(r);
    }
    UNIVERSE = out;
  })();

  // 入选时间落 localStorage：刷新页面后仍在榜的标的保持原入选时刻，
  // 否则每刷一次整榜都会显示「刚刚入选」，那个字段就失去意义了。
  var STORE = null;
  try { STORE = window.localStorage; STORE.setItem('__fm_probe', '1'); STORE.removeItem('__fm_probe'); }
  catch (e) { STORE = null; }
  var SELECT = (window.FMScore && window.FMScore.makeSelectionBook(STORE)) || function () { };

  var BATCH_SIZE = 400;      // 单次 ulist.np 的代码数（实测 300 只可全量返回，留余量）
  var BATCH_CONC = 6;        // 并发批次数
  var TX_BATCH_SIZE = 400;   // 腾讯单批代码数（实测 800 只照返，留一半余量压 URL 长度）
  var TX_BATCH_CONC = 4;

  // 腾讯代码前缀：沪 sh / 深 sz / 北交所 bj
  function txSymbol(symbol) {
    var c = codeOf(symbol), s = String(symbol || '');
    if (s.indexOf('.SH') >= 0 || /^(5|6|9)/.test(c)) return 'sh' + c;
    if (/^(4|8)/.test(c)) return 'bj' + c;
    return 'sz' + c;
  }

  // ---- 备用主通道：腾讯批量报价 ----
  // 东财在部分网络整链不可达（macOS 上 TLS 直接被 RST，http 也一样），
  // 而腾讯这家回 access-control-allow-origin: *、且不校验 Referer，
  // 是「浏览器直连能通、一次又能给几百只」的全市场行情源。
  // 它比东财多给三样东西：涨停价 / 跌停价（封板家数不用再按涨跌幅猜，
  // ST 的 5cm 和北交所的 30cm 都不会算错）、量比、换手率真值。
  var TX_RE = /v_(?:sh|sz|bj)(\d{6})="([^"]*)"/g;
  function txParse(text) {
    var out = {}, m;
    TX_RE.lastIndex = 0;
    while ((m = TX_RE.exec(text))) {
      var p = m[2].split('~');
      if (p.length < 50) continue;
      var price = num(p[3]), prev = num(p[4]);
      if (!(price > 0) || !(prev > 0)) continue;      // 停牌 / 无报价
      var amt = num(p[37]), vol = num(p[6]);
      out[m[1]] = {
        f2: price, f18: prev,
        // 涨跌幅按价格自算，不用源头的 p[32]：服务端那条通道也是这么算的，
        // 两边取不同来源会让涨停判定和追涨惩罚在 0.001 级上漂移。
        f3: Math.round((price / prev - 1) * 100000) / 1000,
        f17: num(p[5]), f15: num(p[33]), f16: num(p[34]),
        f6: amt == null ? null : amt * 1e4,           // 万元 → 元
        f5: vol == null ? null : vol * 100,           // 手 → 股
        f8: num(p[38]), f14: p[1],
        lu: num(p[47]), ld: num(p[48]), vr: num(p[49]),
        // 外盘 / 内盘（手）：主动买、主动卖的成交量。全市场汇总就是盘中
        // 资金流的原始量，不必依赖同花顺那套日频指标。
        outer: num(p[7]), inner: num(p[8])
      };
    }
    return out;
  }

  function txGroup(list) {
    var url = 'https://qt.gtimg.cn/q=' + list.join(',');
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      return r.ok ? r.arrayBuffer() : null;
    }).then(function (buf) {
      if (!buf) return null;
      var text;
      // 上游声明 charset=GBK：必须是先取字节再按 GBK 解，直接 r.text() 会把
      // 中文股票名解成乱码（名称其实走基座，但乱码会污染 f14 回显）。
      try { text = new TextDecoder('gbk').decode(buf); }
      catch (e) { text = new TextDecoder('utf-8').decode(buf); }
      var parsed = txParse(text);
      return Object.keys(parsed).length ? parsed : null;
    }).catch(function () { return null; });
  }

  // 全市场批量行情：按代码列表分批请求，而非 clist 翻页。
  // clist 每页硬上限 100 条，5000 只要 56 个请求；按代码列表一次能带 300+，
  // 同样的覆盖面只要十几个请求，刷新频率才可能做到一分钟级别。
  function emGroup(ids) {
    var cb = 'FM_MK' + Math.random().toString(36).slice(2, 8);
    var url = 'https://push2.eastmoney.com/api/qt/ulist.np/get?cb=' + cb +
      '&fltt=2&invt=2&secids=' + encodeURIComponent(ids.join(',')) +
      '&fields=f2,f3,f6,f8,f12,f14,f15,f16,f17,f18,f100,f184';
    return jsonp(url, cb, 9000).then(function (res) {
      var diff = res && res.data && res.data.diff;
      if (!diff || !diff.length) return null;
      var out = {};
      for (var j = 0; j < diff.length; j++) {
        var r = diff[j], c = String(r.f12 || '');
        if (c.length !== 6) continue;
        var item = {
          f2: num(r.f2), f3: num(r.f3), f6: num(r.f6), f8: num(r.f8),
          f14: r.f14, f15: num(r.f15), f16: num(r.f16), f17: num(r.f17),
          f18: num(r.f18), f100: r.f100
        };
        // 资金流缺失时不要塞 null 占位：applyQuotes 见到 f184 键在就会当成
        // 「资金流是 0」，全市场资金维度会集体归零。让键干脆不存在，
        // 那边会自动退回基座值。
        if (r.f184 !== undefined && r.f184 !== null && r.f184 !== '-') item.f184 = num(r.f184);
        out[c] = item;
      }
      return Object.keys(out).length ? out : null;
    }, function () { return null; });
  }

  // 东财整轮；先只打第一批探路，通了再并发其余批。
  // 不探路的话，东财被墙时 13 批各等 9 秒，一轮刷新要两分钟，页面看着像卡死。
  function emMarketQuotes(secids) {
    var groups = [];
    for (var i = 0; i < secids.length; i += BATCH_SIZE) groups.push(secids.slice(i, i + BATCH_SIZE));
    return emGroup(groups[0]).then(function (first) {
      if (!first || Object.keys(first).length < groups[0].length * 0.5) return null;
      var out = first, okB = 1, idx = 1;
      function next() {
        if (idx >= groups.length) return Promise.resolve();
        var g = groups[idx++];
        return emGroup(g).then(function (q) {
          if (q) {
            okB++;
            for (var k in q) out[k] = q[k];
          }
          return next();
        });
      }
      var workers = [];
      for (var w = 0; w < BATCH_CONC; w++) workers.push(next());
      return Promise.all(workers).then(function () {
        return { quotes: out, ok: okB, total: groups.length, source: 'eastmoney_ulist' };
      });
    });
  }

  function txMarketQuotes(rows) {
    var list = rows.map(function (r) { return txSymbol(r.symbol); });
    var groups = [];
    for (var i = 0; i < list.length; i += TX_BATCH_SIZE) groups.push(list.slice(i, i + TX_BATCH_SIZE));
    var out = {}, idx = 0, okB = 0;
    function next() {
      if (idx >= groups.length) return Promise.resolve();
      var g = groups[idx++];
      return txGroup(g).then(function (q) {
        if (q) {
          okB++;
          for (var k in q) out[k] = q[k];
        }
        return next();
      });
    }
    var workers = [];
    for (var w = 0; w < TX_BATCH_CONC; w++) workers.push(next());
    return Promise.all(workers).then(function () {
      return { quotes: out, ok: okB, total: groups.length, source: 'tencent_qt' };
    });
  }

  function marketQuotes(rows) {
    var secids = rows.map(function (r) { return secid(r.symbol); });
    return emMarketQuotes(secids).then(function (res) {
      if (res) return res;
      return txMarketQuotes(rows);
    });
  }

  // 盘口广度：涨跌家数、涨跌停、两市成交额、均涨/中位。
  // 收盘后行情定格，这里算出来的自然就是收盘广度，不需要另写一套收盘逻辑。
  function breadthOf(rows, ph, quotes) {
    var up = 0, down = 0, flat = 0, lu = 0, ld = 0, strong = 0, weak = 0;
    var amount = 0, pcts = [], exact = 0;
    var buyAmt = 0, sellAmt = 0, flowN = 0;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i], pct = r.change_pct;
      if (pct === null || pct === undefined || !isFinite(pct)) continue;
      pcts.push(pct);
      if (pct > 0.001) up++; else if (pct < -0.001) down++; else flat++;
      // 源头给了涨跌停价就按价判：ST 的 5cm、北交所的 30cm 都不会算错。
      // 只有拿不到涨跌停价（东财字段里没有）时才退回 9.8% 近似，
      // 那时 ST 的封板会被漏计，所以要把口径如实标出来。
      var pe = Number(r.price), luP = Number(r.limit_up_price), ldP = Number(r.limit_down_price);
      if (luP > 0 && isFinite(pe)) {
        exact++;
        if (pe >= luP - 0.005) lu++;
        else if (ldP > 0 && pe <= ldP + 0.005) ld++;
      } else if (pct >= 9.8) lu++; else if (pct <= -9.8) ld++;
      if (pct >= 5) strong++; else if (pct <= -5) weak++;
      var a = Number(r.amount);
      if (isFinite(a)) amount += a;
      // 主动买/主动卖：靠外盘、内盘汇总。腾讯给这两个字段，东财不给，
      // 所以资金流只在腾讯通道下才有值，没有就留 null，不能填 0 冒充。
      var q = quotes ? quotes[String(r.code)] : null;
      if (q && q.outer > 0 && q.inner > 0 && q.f6 > 0 && q.f5 > 0) {
        var avgPx = q.f6 / q.f5;                 // 成交额 / 成交量 = 均价（元/股）
        buyAmt += q.outer * 100 * avgPx;          // 外盘单位是手
        sellAmt += q.inner * 100 * avgPx;
        flowN++;
      }
    }
    var total = pcts.length;
    if (!total) return { error: '行情无涨跌幅字段' };
    pcts.sort(function (a, b) { return a - b; });
    var sum = 0;
    for (var k = 0; k < total; k++) sum += pcts[k];
    return {
      count: total, up: up, down: down, flat: flat,
      limit_up: lu, limit_down: ld, strong: strong, weak: weak,
      amount_yi: Math.round(amount / 1e6) / 100,
      amount_label: ph.key === 'auction' ? '竞价成交' : '两市成交',
      avg_change: Math.round(sum / total * 100) / 100,
      median_change: Math.round(pcts[Math.floor(total / 2)] * 100) / 100,
      ratio: Math.round(up / Math.max(1, down) * 100) / 100,
      session: ph.key, session_label: ph.label,
      live: ph.live, score_ready: ph.scored,
      limit_exact: exact > total * 0.5,
      flow_net_yi: flowN ? Math.round((buyAmt - sellAmt) / 1e6) / 100 : null,
      flow_buy_yi: flowN ? Math.round(buyAmt / 1e6) / 100 : null,
      flow_sell_yi: flowN ? Math.round(sellAmt / 1e6) / 100 : null,
      flow_ratio: (flowN && amount) ? Math.round((buyAmt - sellAmt) / amount * 10000) / 100 : null,
      flow_count: flowN,
      flow_basis: '主动买卖（外盘-内盘），全市场现算',
      quote_at: MARKET_AT, cached: false
    };
  }

  // 把内部状态写在 <html data-fm-*> 上：不占界面，但无头浏览器 / 冒烟测试
  // 一眼就能看出「基座有没有加载、行情通不通、评分有没有真重算」，
  // 不用去猜页面上那句话到底是被渲染了还是被兜底了。
  function markState() {
    try {
      var ds = document.documentElement.dataset;
      ds.fmMode = 'static';
      ds.fmUniverse = String(UNIVERSE.length);
      ds.fmBaseAt = (BASE && BASE.snapshot_at) || '';
      ds.fmLive = FEED.live ? '1' : '0';
      ds.fmScored = MARKET_SCORED ? '1' : '0';
      ds.fmSource = FEED.source || '';
      ds.fmHit = String(FEED.hit || 0);
      ds.fmPhase = PHASE ? PHASE.key : '';
      ds.fmReason = FEED.reason || '';
    } catch (e) { /* 极端环境（无 DOM）忽略 */ }
  }

  function refreshLegacy() {
    var symbols = FEED.rows.map(function (r) { return r.symbol; }).concat(EXTRA);
    if (!symbols.length) return Promise.resolve();
    return emBatch(symbols).then(function (em) {
      var emCount = em ? Object.keys(em).length : 0;
      var need = symbols.filter(function (s) { return !em || !em[codeOf(s)]; }).map(codeOf);
      if (emCount >= symbols.length * 0.6 || !need.length) {
        FEED.quotes = em || {};
        FEED.source = 'eastmoney_ulist';
        FEED.warning = emCount < symbols.length ? '部分代码未返回报价，其余沿用快照价格' : '';
        return;
      }
      // 东财整链不可用时先走腾讯：同花顺是逐只请求（100 只就是 100 个请求），
      // 腾讯一批 400 只，而且和东财一样不受 CORS 限制。同花顺退到最后。
      return txBatch(need).then(function (tx) {
        var txCount = Object.keys(tx).length;
        if (txCount >= need.length * 0.6) {
          var merged = Object.assign({}, tx);
          // 已拿到的东财结果优先保留：它带实时资金流，腾讯没有
          Object.keys(em || {}).forEach(function (c) { merged[c] = Object.assign({}, tx[c], em[c]); });
          FEED.quotes = merged;
          FEED.source = 'tencent_qt';
          FEED.warning = txCount < need.length ? '部分代码未返回报价，其余沿用快照价格' : '';
          return;
        }
        return thsBatch(need, 20).then(function (ths) {
          var m2 = Object.assign({}, ths);
          Object.keys(em || {}).forEach(function (c) { m2[c] = Object.assign({}, ths[c], em[c]); });
          FEED.quotes = m2;
          FEED.source = Object.keys(ths).length ? 'ths_realhead' : 'partial';
          FEED.warning = '东财与腾讯批量报价都不可用，价格来自同花顺（分钟级），资金流仍为快照值';
        });
      });
    }).catch(function () { FEED.quotes = {}; FEED.source = 'unavailable'; })
      .then(function () {
        applyQuotes();
        var ok = FEED.hit >= FEED.total * 0.6;
        FEED.live = ok;
        FEED.reason = ok ? '浏览器直连行情' : '行情源暂不可用，已回退快照';
        FEED.at = bjIso();
        FEED.failStreak = ok ? 0 : FEED.failStreak + 1;
        FEED.digest = buildDigest();
        markState();
        if (typeof window.FM.onTick === 'function') window.FM.onTick();
      });
  }

  // 全市场路径：一次拉五千只，同时喂「盘口广度」和「七维重排」两件事。
  function refreshMarket(ph) {
    return marketQuotes(UNIVERSE).then(function (res) {
      var hit = Object.keys(res.quotes).length;
      var total = UNIVERSE.length;
      FEED.at = bjIso();
      MARKET_AT = FEED.at;
      FEED.hit = hit;
      FEED.total = total;
      if (hit < total * 0.5) {
        // 大面积失败时不拿半份数据重排：那会算出一份看起来正常、实际漏了半个市场的榜单
        FEED.live = false;
        FEED.source = 'partial';
        // 只写 reason：warning 会和 reason 一起渲染，两份同文案会重复显示
        FEED.warning = '';
        FEED.reason = '行情源返回不足（' + hit + '/' + total + '），榜单沿用上一轮';
        FEED.failStreak++;
        FEED.digest = buildDigest();
        markState();
        if (typeof window.FM.onTick === 'function') window.FM.onTick();
        return;
      }
      FEED.failStreak = 0;
      FEED.quotes = res.quotes;
      FEED.source = res.source || 'eastmoney_ulist';
      FEED.live = true;
      FEED.reason = ph.label;
      FEED.warning = res.ok < res.total ? '有 ' + (res.total - res.ok) + ' 批行情未返回，成分沿用基座值' : '';

      var applied = window.FMScore.applyQuotes(UNIVERSE, res.quotes);
      var live = applied.rows;
      MARKET_BREADTH = breadthOf(live, ph, res.quotes);

      if (ph.scored) {
        var market = window.FMScore.fn((BOOT.ranking || {}).market_score, 58.35);
        var ranked = window.FMScore.rank(live, {
          market_score: market, session: 'close', config: SCORE_CFG, limit: 100
        });
        SELECT(ranked.ranking, 100);
        FEED.rows = ranked.ranking;
        MARKET_STATS = ranked.stats;
        MARKET_SCORED = true;
      } else {
        // 集合竞价 / 未开盘：盘口照刷，评分沿用构建时的定稿
        MARKET_SCORED = false;
      }

      // 报告里点开的个股不一定在 Top100 里，行情单独带一份出去
      FEED.extraQuotes = {};
      EXTRA.forEach(function (sym) {
        var q = res.quotes[codeOf(sym)];
        if (!q) return;
        FEED.extraQuotes[sym] = {
          price: q.f2, change_pct: q.f3, money_flow_pct: q.f184,
          turnover_pct: q.f8, sector: q.f100
        };
      });

      FEED.digest = buildDigest();
      markState();
      if (typeof window.FM.onTick === 'function') window.FM.onTick();
    }).catch(function (e) {
      FEED.live = false;
      FEED.source = 'unavailable';
      FEED.reason = '行情源不可用：' + String((e && e.message) || e);
      FEED.failStreak++;
      markState();
      if (typeof window.FM.onTick === 'function') window.FM.onTick();
    });
  }

  function refresh() {
    PHASE = window.FMScore.phase();
    // 有全市场基座就走重排路径；没有（CI 采集失败、仓库未同步快照）就退回
    // 「只刷榜单那 100 只的行情」——宁可说清楚能力有限，也不假装能重排。
    if (UNIVERSE.length) return refreshMarket(PHASE);
    return refreshLegacy();
  }

  // 刷新节奏跟着时段走：交易时段一分钟，其余时间拉一次就够，
  // 没必要让一个后台标签页每分钟耗掉一兆流量。
  function schedule() {
    var ms = PHASE.live ? 60000 : 300000;
    setTimeout(function () {
      refresh().catch(function () { }).then(schedule);
    }, ms);
  }

  // ---- 核心指标：浏览器直连同花顺 dapan_v3 ----
  // 该接口返回 Access-Control-Allow-Origin: *，静态页也能现拉，不依赖任何后端。
  // 指标本身是日频（收盘后补当日点），所以只照实显示每条自己的数据日期，
  // 不把昨天的值标成今天的。
  var MET_SPEC = {
    sjdp_market_capital: ['大盘资金', 'net_inflow', ['net_inflow', 'szzz']],
    sjdp_north_capital: ['北向资金', 'net_inflow', ['net_purchase', 'net_inflow', 'szzz']],
    sjdp_temperature_hs: ['市场情绪', 'temperature', ['temperature']],
    sjdp_rzrq: ['融资融券', 'jlr', ['lrye', 'jlr', 'szzz']],
    sjdp_new_investors: ['新增开户', 'invest', ['invest', 'szzz']],
    sjdp_market_financing: ['市场融资', 'bond', ['ipo', 'add', 'bond', 'szzz']],
    sjdp_lift_the_ban: ['限售解禁', 'lift_the_ban', ['lift_the_ban', 'szzz']],
    sjdp_increase_or_decrease: ['增减持', 'zjc', ['zjc', 'szzz']],
    sjdp_new_fund_size: ['新增基金', 'new_fund_size', ['new_fund_size', 'szzz']],
    sjdp_reverse_repurchase: ['逆回购', 'jtf', ['jtf', 'jhg', 'szzz']],
    sjdp_dollar_rmb: ['美元人民币', 'dollar_rmb', ['dollar_rmb', 'szzz']]
  };
  var FIELD_LABELS = {
    net_inflow: '净流入', net_purchase: '净买额', szzz: '上证指数', temperature: '市场温度',
    lrye: '两融余额', jlr: '净流入', invest: '新增开户数', ipo: 'IPO 募资', add: '增发',
    bond: '债券融资', lift_the_ban: '解禁规模', zjc: '增减持', new_fund_size: '新增基金规模',
    jtf: '净投放', jhg: '净回购', dollar_rmb: '美元/人民币'
  };
  var SIGNED = { net_inflow: 1, net_purchase: 1, jlr: 1, zjc: 1, jtf: 1, jhg: 1 };
  var MET_BASE = 'https://eq.10jqka.com.cn/open/api/dapan_v3/chart/';
  var MET_TTL = 120000;
  var MET_KEYS = Object.keys(MET_SPEC);
  var MET = { at: 0, data: null };

  function mNum(v) { var n = Number(v); return (v === null || v === '' || !isFinite(n)) ? null : n; }
  function mTone(f, v) {
    var n = mNum(v);
    if (f === 'temperature' && n !== null) return n >= 70 ? 'up' : n <= 30 ? 'down' : 'flat';
    if (SIGNED[f] && n !== null) return n > 0 ? 'up' : n < 0 ? 'down' : 'flat';
    return 'flat';
  }
  function mDisplay(f, v) {
    if (v === null || v === '') return '—';
    var n = mNum(v); if (n === null) return String(v);
    var raw = String(v).trim();
    var text = raw.indexOf('.') >= 0 ? n.toFixed(2) : String(Math.round(n));
    var parts = text.split('.'), int = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    text = parts.length > 1 ? int + '.' + parts[1] : int;
    if (SIGNED[f] && n > 0) text = '+' + text;
    return text;
  }
  function mDate(raw) {
    var s = String(raw == null ? '' : raw).trim(), full = s, short = s, parsed = null, monthly = false;
    if (/^\d{8}$/.test(s)) {
      parsed = new Date(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8));
      full = s.slice(0, 4) + '-' + s.slice(4, 6) + '-' + s.slice(6, 8);
      short = s.slice(4, 6) + '-' + s.slice(6, 8);
    } else if (/^\d{6}$/.test(s)) {
      full = s.slice(0, 4) + '-' + s.slice(4, 6); short = full; monthly = true;
      parsed = new Date(+s.slice(0, 4), +s.slice(4, 6) - 1, 1);
    }
    var now = new Date(), today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return {
      date_full: full, date_text: short, monthly: monthly,
      future: !!(parsed && parsed > today),
      stale: !!(parsed && (today - parsed) / 86400000 > (monthly ? 95 : 30))
    };
  }
  // 与 metric_display._asof 同口径：今日 / 昨收 / N日前 / 停更 / 预告
  function mAsOf(info, noData) {
    if (noData) return { asof_label: '未披露', asof_today: false };
    if (info.future) return { asof_label: '预告', asof_today: false };
    if (info.stale) return { asof_label: '停更', asof_today: false };
    if (!info.date_full) return { asof_label: '', asof_today: false };
    var s = String(info.date_full).replace(/-/g, '');
    var parsed;
    if (s.length === 8) parsed = new Date(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8));
    else if (s.length === 6) parsed = new Date(+s.slice(0, 4), +s.slice(4, 6) - 1, 1);
    else return { asof_label: '', asof_today: false };
    var now = new Date(), today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var days = Math.round((today - parsed) / 86400000);
    var label;
    if (days === 0) label = '今日';
    else if (info.monthly) label = days <= 31 ? '本月' : Math.max(1, Math.round(days / 30)) + '月前';
    else {
      var prev = new Date(today.getTime() - 86400000);
      while (prev.getDay() === 0 || prev.getDay() === 6) prev = new Date(prev.getTime() - 86400000);
      if (+parsed === +prev) label = '昨收';
      else label = days <= 3 ? days + '日前' : '历史';
    }
    return { asof_label: label, asof_today: days === 0 };
  }
  function buildMetric(key, raw) {
    if (!raw || !MET_SPEC[key]) return null;
    var spec = MET_SPEC[key], label = spec[0], mainField = spec[1];
    var items = spec[2].filter(function (f) { return f in raw; }).map(function (f) {
      return { key: f, label: FIELD_LABELS[f] || f, value: mDisplay(f, raw[f]),
               tone: mTone(f, raw[f]), empty: raw[f] === null || raw[f] === '' };
    });
    var chosen = null;
    items.forEach(function (i) { if (!chosen && i.key === mainField && !i.empty) chosen = i; });
    if (!chosen) items.forEach(function (i) { if (!chosen && i.key !== 'szzz' && !i.empty) chosen = i; });
    var no_data = !chosen;
    var info = mDate(raw.date), hint = '';
    if (no_data) hint = '本期未披露';
    else if (info.future) hint = '预告';
    else if (info.stale) hint = '历史数据';
    else if (chosen && chosen.key === 'temperature') {
      var t = mNum(raw.temperature);
      hint = (t !== null && t >= 70) ? '偏热' : (t !== null && t <= 30) ? '偏冷' : '中性';
    }
    return {
      label: label, main_label: no_data ? '本期披露' : (FIELD_LABELS[chosen.key] || '数值'),
      main_value: no_data ? '暂无数据' : chosen.value, main_tone: no_data ? 'flat' : chosen.tone,
      main_key: chosen ? chosen.key : mainField, no_data: no_data, hint: hint,
      date_text: info.date_text, date_full: info.date_full, items: items,
      asof_label: mAsOf(info, no_data).asof_label,
      asof_today: mAsOf(info, no_data).asof_today
    };
  }
  // 排期类指标：解禁看的是「下一批什么时候来」，取最近一个未来点并标「预告」
  var FUTURE_PREFERRED = { sjdp_lift_the_ban: 1 };

  // 北京时间今天 YYYYMMDD（指标日期是不带分隔的 8 位数字）
  function bjToday() {
    return bjIso().slice(0, 10).replace(/-/g, '');
  }

  // 取「该显示的那一个点」，规则与 daily_runner.latest_chart 一致：
  // 不能无脑取末条——限售解禁返回的是未来排期，市场融资夹带未来占位行，
  // 取末条会把「三个月后解禁 346 亿」当成今天的解禁规模显示。
  function pickPoint(key, keys, points) {
    var valueKeys = keys.filter(function (k) { return k !== 'date' && k !== 'szzz'; });
    if (!valueKeys.length) valueKeys = keys.filter(function (k) { return k !== 'date'; });
    var today = bjToday();
    function isZero(v) {
      if (v === null || v === undefined || v === '') return true;
      var n = Number(v);
      return isFinite(n) && n === 0;
    }
    var dated = [];
    points.forEach(function (p) {
      var row = {};
      keys.forEach(function (k, i) { row[k] = p[i]; });
      var vals = valueKeys.map(function (k) { return row[k]; });
      if (vals.every(function (v) { return v === null || v === undefined || v === ''; })) return;
      var d = String(row.date || '');
      // 未来日期且各字段全 0 的是占位行，不是真实披露
      if (d > today && vals.every(isZero)) return;
      dated.push([d, row]);
    });
    if (!dated.length) return null;
    if (!dated.some(function (x) { return x[0]; })) return dated[dated.length - 1][1];
    var future = dated.filter(function (x) { return x[0] > today; })
      .sort(function (a, b) { return a[0] < b[0] ? -1 : 1; });
    var past = dated.filter(function (x) { return x[0] && x[0] <= today; })
      .sort(function (a, b) { return a[0] < b[0] ? 1 : -1; });
    if (FUTURE_PREFERRED[key] && future.length) return future[0][1];
    if (past.length) return past[0][1];
    if (future.length) return future[0][1];
    return dated[dated.length - 1][1];
  }

  function metricsLive() {
    if (MET.data && Date.now() - MET.at < MET_TTL) {
      // 缓存里那份 metrics 是日频，可以复用；盘口广度是分钟级，必须用当前值
      return Promise.resolve(Object.assign({}, MET.data, { breadth: MARKET_BREADTH }));
    }
    return Promise.all(MET_KEYS.map(function (key) {
      return fetch(MET_BASE + key + '.json', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (body) {
          var d = (body && body.data) || {}, ks = d.point_key_list || [], pts = d.point_list || [];
          if (!pts.length) return null;
          var out = pickPoint(key, ks, pts);
          if (!out) return null;
          out = Object.assign({}, out);
          out.__key = key;
          return out;
        })
        .catch(function () { return null; });
    })).then(function (rows) {
      var raw = {}, ok = 0;
      rows.forEach(function (r) { if (r) { raw[r.__key] = r; ok++; } });
      if (!ok) {
        return { metrics: [], error: '指标接口不可达（浏览器直连被拦）', breadth: MARKET_BREADTH };
      }
      var data = {
        metrics: MET_KEYS.map(function (k) { return buildMetric(k, raw[k]); }).filter(Boolean),
        // 盘口广度由全市场行情现算（见 refreshMarket），不再拿榜单那 100 只凑数
        breadth: MARKET_BREADTH || { unavailable: true, note: '盘口广度：等待行情首次返回' },
        fetched_at: bjIso(), source: '同花顺 dapan_v3（浏览器直连）', count: ok, cached: false
      };
      MET.at = Date.now(); MET.data = data;
      return data;
    });
  }

  // ---- 与 dashboard_server.py 同形的接口 ----
  window.FM = {
    mode: 'static',
    onTick: null,
    get: function (path) {
      var url = String(path || ''), q = url.indexOf('?');
      var qs = new URLSearchParams(q >= 0 ? url.slice(q + 1) : '');
      if (url.indexOf('/api/metrics') === 0) return metricsLive();
      if (url.indexOf('/api/reports') === 0) return Promise.resolve({ reports: BOOT.reports || {} });
      if (url.indexOf('/api/status') === 0) return Promise.resolve(BOOT.status || {});
      if (url.indexOf('/api/ranking') === 0) {
        var limit = Math.max(1, Math.min(parseInt(qs.get('limit') || '30', 10) || 30, 100));
        var base = BOOT.ranking || {};
        // 重排是否生效，取决于「有没有全市场基座」+「这一轮行情够不够」+「时段允不允许」，
        // 三者缺一就如实退回定稿口径，不把一份不会动的榜单标成实时。
        var ranked = MARKET_SCORED && FEED.live;
        return Promise.resolve(Object.assign({}, base, {
          ranking: FEED.rows.slice(0, limit),
          returned: Math.min(limit, FEED.rows.length),
          available: MARKET_STATS ? MARKET_STATS.count : FEED.rows.length,
          stats: MARKET_STATS || base.stats,
          live: FEED.live, live_reason: FEED.reason, quote_at: FEED.at,
          quote_source: FEED.source, live_warning: FEED.warning,
          live_merge: { hit: FEED.hit, total: FEED.total, elapsed: window.FMScore.elapsedRatio() },
          breadth: MARKET_BREADTH,
          ranking_live: ranked,
          ranking_at: ranked ? MARKET_AT : (base.generated_at || ''),
          ranking_basis: ranked ? 'intraday_recalc' : (base.ranking_basis || 'defined_snapshot'),
          flow_source: ranked ? 'live' : (base.flow_source || 'defined'),
          base_at: (BASE && BASE.snapshot_at) || '',
          extra_quotes: FEED.extraQuotes,
          feed_digest: FEED.digest, source_label: BOOT.source_label || '示例快照'
        }));
      }
      return Promise.resolve({ error: '在线演示版不支持该接口' });
    },
    post: function () {
      return Promise.resolve({ ok: false, error: '在线演示版不触发流水线；请在本地或 VPS 看板运行' });
    },
    // 给页面用的能力自述：有没有全市场基座、能不能重排
    capability: function () {
      return { universe: UNIVERSE.length, can_rescore: !!(UNIVERSE.length && BASE && BASE.rows.length),
               base_at: (BASE && BASE.snapshot_at) || '', phase: PHASE };
    }
  };

  // 首次立即拉一轮，之后按「交易时段 1 分钟 / 其余 5 分钟」自适应
  refresh().catch(function () { }).then(schedule);
})();
