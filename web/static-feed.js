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
    out.push({ kind: 'warn', text: '在线演示版：价格 / 涨跌 / 资金流为浏览器直连实时行情，评分沿用最近一次快照评分' });
    return out;
  }

  function refresh() {
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
      // 东财整链不可用时，逐只切同花顺；已拿到的东财结果优先保留资金流字段
      return thsBatch(need, 20).then(function (ths) {
        var merged = Object.assign({}, ths);
        Object.keys(em || {}).forEach(function (c) { merged[c] = Object.assign({}, ths[c], em[c]); });
        FEED.quotes = merged;
        FEED.source = Object.keys(ths).length ? 'ths_realhead' : 'partial';
        FEED.warning = '东财批量报价不可用，价格来自同花顺（分钟级），资金流仍为快照值';
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
        if (typeof window.FM.onTick === 'function') window.FM.onTick();
      });
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
      date_full: full, date_text: short,
      future: !!(parsed && parsed > today),
      stale: !!(parsed && (today - parsed) / 86400000 > (monthly ? 95 : 30))
    };
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
      date_text: info.date_text, date_full: info.date_full, items: items
    };
  }
  function metricsLive() {
    if (MET.data && Date.now() - MET.at < MET_TTL) return Promise.resolve(MET.data);
    return Promise.all(MET_KEYS.map(function (key) {
      return fetch(MET_BASE + key + '.json', { cache: 'no-store' })
        .then(function (r) { return r.json(); })
        .then(function (body) {
          var d = (body && body.data) || {}, ks = d.point_key_list || [], pts = d.point_list || [];
          if (!pts.length) return null;
          var row = pts[pts.length - 1], out = {};
          ks.forEach(function (k, i) { out[k] = row[i]; });
          out.__key = key;
          return out;
        })
        .catch(function () { return null; });
    })).then(function (rows) {
      var raw = {}, ok = 0;
      rows.forEach(function (r) { if (r) { raw[r.__key] = r; ok++; } });
      if (!ok) return { metrics: [], error: '指标接口不可达（浏览器直连被拦）' };
      var data = {
        metrics: MET_KEYS.map(function (k) { return buildMetric(k, raw[k]); }).filter(Boolean),
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
        return Promise.resolve(Object.assign({}, base, {
          ranking: FEED.rows.slice(0, limit),
          returned: Math.min(limit, FEED.rows.length), available: FEED.rows.length,
          live: FEED.live, live_reason: FEED.reason, quote_at: FEED.at,
          quote_source: FEED.source, live_warning: FEED.warning,
          live_merge: { hit: FEED.hit, total: FEED.total, elapsed: 1 },
          // live 只代表「行情是实时的」，不代表「排名被重排过」。静态页没有
          // 后端算七维评分，榜单成分/名次是构建那一刻的定稿，必须如实区分，
          // 否则会把一份不会动的榜单标成「● 实时」让人误判。
          ranking_live: false,
          ranking_at: base.generated_at || '',
          ranking_basis: base.ranking_basis || 'defined_snapshot',
          flow_source: base.flow_source || 'defined',
          extra_quotes: FEED.extraQuotes,
          feed_digest: FEED.digest, source_label: BOOT.source_label || '示例快照'
        }));
      }
      return Promise.resolve({ error: '在线演示版不支持该接口' });
    },
    post: function () {
      return Promise.resolve({ ok: false, error: '在线演示版不触发流水线；请在本地或 VPS 看板运行' });
    }
  };

  setTimeout(refresh, 400);
  setInterval(refresh, REFRESH_MS);
})();
