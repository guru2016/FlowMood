/* FlowMood 七维评分引擎（浏览器 / Node 通用）
 *
 * 为什么要有这个文件：本地看板能「盘中实时重排」，靠的是 dashboard_server.py
 * 在服务端跑 stock_selector.score_stock；GitHub Pages 是纯静态托管，没有服务端。
 * 过去静态页只能把构建那一刻的榜单原样显示，价格刷新但名次不动——看起来就像
 * 「系统没更新」。这里把同一套评分公式原样搬到浏览器：日线统计字段（atr14 /
 * ma20 / high20 / pivot / trend_consistency）随页面下发，价格类字段现拉行情，
 * 两者一拼就能在浏览器里对全市场重算，结果与服务端同源。
 *
 * 移植纪律：公式、默认值、缺失维度处理、闸门顺序必须与 stock_selector.py
 * 逐行对应。任何「顺手优化」都会让 Pages 与本地算出不同的分，那就失去了
 * 两边可比的前提。tests/score_parity.py 就是拿两边逐只对账用的。
 */
(function (root) {
  'use strict';

  // ---------- 数值原语 ----------
  // 对应 Python 的 _f()：键不存在或值不可转数字时返回默认值。
  // 空字符串要单独排除，因为 Number('') === 0 会把它当成真实的 0。
  function fn(v, d) {
    if (v === null || v === undefined || v === '') return d;
    var n = Number(v);
    return isFinite(n) ? n : d;
  }
  function has(row, key) {
    return Object.prototype.hasOwnProperty.call(row, key) && row[key] !== null && row[key] !== undefined;
  }
  // 指数饱和：x=half 时为 0.5，x→∞ 渐近 1。用于资金流这类无上界指标。
  function sat(x, half) {
    if (half <= 0) return 0;
    return 1 - Math.pow(2, -Math.max(0, x) / half);
  }
  // 分段线性归一：<=lo 归零，>=hi 满分。用于本身上下有界的指标。
  function ramp(x, lo, hi) {
    if (hi <= lo) return x >= hi ? 1 : 0;
    return Math.min(1, Math.max(0, (x - lo) / (hi - lo)));
  }
  // 量能最优区间：1.2~3 倍健康放量，过低无量能，过高多为冲高博弈。
  function volumeBand(ar, maxRatio) {
    if (ar <= 0) return 0;
    if (ar < 1.2) return 0.8 * ramp(ar, 0.5, 1.2);
    if (ar <= 3.0) return 1;
    return Math.max(0.25, 1 - 0.75 * (ar - 3.0) / Math.max(maxRatio - 3.0, 1e-9));
  }
  // 换手率最优区间：2%~18%。
  function turnoverBand(t) {
    if (t <= 0) return 0;
    if (t < 2) return ramp(t, 0.5, 2);
    if (t <= 18) return 1;
    return Math.max(0.2, 1 - (t - 18) / 30);
  }
  function clip(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function round(v, n) { var p = Math.pow(10, n); return Math.round(v * p) / p; }

  var DEFAULT_WEIGHTS = { trend: 22, flow: 20, sector: 18, setup: 14, sentiment: 12, liquidity: 8, market: 6 };
  var DIM_LABELS = [['trend', '趋势'], ['flow', '资金'], ['sector', '板块'], ['setup', '结构'],
                    ['sentiment', '情绪'], ['liquidity', '流动'], ['market', '大盘']];

  function scoreWeights(cfg) {
    var w = Object.assign({}, DEFAULT_WEIGHTS);
    var src = (cfg && cfg.score_weights) || {};
    Object.keys(src).forEach(function (k) {
      if (k in w) { var v = Number(src[k]); if (isFinite(v)) w[k] = v; }
    });
    var total = 0;
    Object.keys(w).forEach(function (k) { total += w[k]; });
    if (total <= 0) return Object.assign({}, DEFAULT_WEIGHTS);
    var out = {};
    Object.keys(w).forEach(function (k) { out[k] = w[k] * 100 / total; });
    return out;
  }

  // ---------- 板块聚合（对应 sector_scoring.enrich）----------
  // 板块分只由该板块成分股的实时涨跌/资金/量能/封板算出，所以盘中能跟着变。
  function enrich(rows, strongMin) {
    var groups = {};
    rows.forEach(function (r) {
      var k = String(r.sector || '未分类');
      (groups[k] = groups[k] || []).push(r);
    });
    var scores = {};
    Object.keys(groups).forEach(function (k) {
      var items = groups[k], n = items.length;
      var breadth = items.filter(function (x) { return fn(x.change_pct, 0) > 0; }).length / n;
      var money = items.reduce(function (a, x) { return a + fn(x.money_flow_pct, 0); }, 0) / n;
      var amount = items.reduce(function (a, x) { return a + fn(x.amount_ratio, 1); }, 0) / n;
      var limitups = items.filter(function (x) { return !!x.limit_up; }).length;
      var s = breadth * 45 + clip(money, -10, 10) * 2 + Math.min(2, amount - 1) * 15
              + Math.min(3, limitups) * 5 + 20;
      scores[k] = round(clip(s, 0, 100), 2);
    });
    var strong = 0;
    Object.keys(scores).forEach(function (k) { if (scores[k] >= (strongMin || 70)) strong++; });
    return { scores: scores, strong: strong };
  }

  // 交易时钟一律按北京时间：看板是给 A 股用的，用户浏览器可能在别的时区，
  // 直接用 getHours() 会让「已交易时长」算错，量比折算跟着整体失真。
  function bjClock(d) {
    var t = new Date((d ? d.getTime() : Date.now()) + (d ? d.getTimezoneOffset() * 60000 : new Date().getTimezoneOffset() * 60000) + 8 * 3600000);
    return { day: t.getDay(), minutes: t.getHours() * 60 + t.getMinutes() };
  }

  // 当日已交易时长占全天 240 分钟的比例，把盘中成交额折算成全天口径。
  // 下限 0.15 是刻意的：开盘头几分钟分母太小，折算会把量比放大到失真。
  function elapsedRatio(now, floor) {
    floor = floor === undefined ? 0.15 : floor;
    var c = bjClock(now);
    if (c.day === 0 || c.day === 6) return 1;
    var m = c.minutes;
    if (m < 570 || m > 900) return 1;          // < 09:30 或 > 15:00
    var raw;
    if (m <= 690) raw = m - 570;               // 09:30–11:30
    else if (m < 780) raw = 120;               // 午间休市
    else raw = 120 + (m - 780);                // 13:00–15:00
    return Math.max(floor, Math.min(1, raw / 240));
  }

  // 时段判定：live=能不能看盘口，scored=能不能重算七维评分。
  // 拆成两个字段是因为集合竞价有价格没成交额，那时重排会把流动性维度打崩；
  // 本地 dashboard_server 用同样的分工（is_trading_now / session_phase）。
  // 注意 pre_open / 休市 是 scored=true 的：那时行情返回的是最近一个交易日的
  // 完整收盘数据，重排出来就是「最近收盘榜」，比构建那一刻冻结的榜单更贴近现实。
  function phase(now) {
    var c = bjClock(now), m = c.minutes;
    if (c.day === 0 || c.day === 6) {
      return { key: 'closed', label: '休市·上周末收盘', live: false, scored: true };
    }
    if (m < 555) return { key: 'pre_open', label: '未开盘·昨收数据', live: false, scored: true };
    if (m < 570) return { key: 'auction', label: '集合竞价', live: true, scored: false };
    if (m <= 690) return { key: 'trading', label: '早盘', live: true, scored: true };
    if (m < 780) return { key: 'break', label: '午间休市·上午收盘', live: true, scored: true };
    if (m <= 900) return { key: 'trading', label: '午盘', live: true, scored: true };
    return { key: 'closed', label: '已收盘', live: true, scored: true };
  }

  // ---------- 实时行情覆盖（对应 live_quotes.apply_quotes）----------
  // 只覆盖价格类字段；atr14 / ma20 / high20 / avg_amount20 / trend_consistency
  // 要日K才能算，当天不变，保留基座里的值。
  function applyQuotes(rows, quotes, elapsed) {
    var el = elapsed === undefined || elapsed === null ? elapsedRatio() : elapsed;
    var hit = 0, miss = 0;
    var out = rows.map(function (base) {
      var code = String(base.code || String(base.symbol || '').split('.')[0]);
      var item = quotes[code];
      if (!item) { miss++; return base; }
      var price = fn(item.f2, 0), prev = fn(item.f18, 0);
      if (!(price > 0) || !(prev > 0)) { miss++; return base; }
      hit++;
      var amount = fn(item.f6, 0);
      var changePct = fn(item.f3, 0);
      var moneyFlow;
      if ('f184' in item || 'f62' in item) {
        moneyFlow = fn(item.f184, 0);
        if (!moneyFlow) {
          var net = fn(item.f62, 0);
          moneyFlow = amount > 0 ? net / amount * 100 : 0;
        }
      } else {
        // 无资金流的源（如逐只兜底）：保留基座值，否则全市场资金维度会集体归零
        moneyFlow = fn(base.money_flow_pct, 0);
      }
      var avg20 = fn(base.avg_amount20, 0);
      var amountRatio = avg20 > 0 ? amount / (avg20 * el) : 1.0;
      var cap = /^(30|68)/.test(code) ? 19.5 : 9.5;
      // 涨停价由源头部直给（腾讯 q= 的第 48/49 个字段），有它就按价判：
      // 涨跌幅近似对 ST（5cm）和北交所（30cm）是错的，按价判才不漏不多。
      var luP = fn(item.lu, 0), ldP = fn(item.ld, 0);
      var row = Object.assign({}, base);
      row.price = round(price, 4);
      row.prev_close = round(prev, 4);
      row.high = round(fn(item.f15, price) || price, 4);
      row.low = round(fn(item.f16, price) || price, 4);
      row.open = round(fn(item.f17, price) || price, 4);
      row.amount = round(amount, 2);
      row.change_pct = round(changePct, 4);
      row.turnover_pct = round(fn(item.f8, 0), 4);
      row.money_flow_pct = round(moneyFlow, 4);
      row.amount_ratio = round(amountRatio, 4);
      row.breakout_volume_ratio = round(amountRatio, 4);
      row.sector = String(item.f100 || base.sector || '未分类');
      row.limit_up_price = luP > 0 ? round(luP, 4) : null;
      row.limit_down_price = ldP > 0 ? round(ldP, 4) : null;
      row.limit_up = luP > 0 ? price >= luP - 0.005 : changePct >= cap;
      return row;
    });
    return { rows: out, hit: hit, miss: miss, elapsed: el };
  }

  // ---------- 单只评分（对应 stock_selector.score_stock）----------
  // wantReasons=false 时跳过理由拼装：全市场 5000 只每轮都拼二十条字符串会明显拖慢，
  // 而理由只有进榜的那一百来只才显示。
  function scoreOne(row, marketScore, session, cfg, wantReasons) {
    cfg = cfg || {};
    var price = fn(row.price, 0);
    var prev = fn(row.prev_close, price);
    var high = fn(row.high, price);
    var low = fn(row.low, price);
    var amountRatio = fn(row.amount_ratio, 1.0);
    var moneyFlow = fn(row.money_flow_pct, 0);
    var sectorScore = fn(row.sector_score, 0);
    var sentiment = fn(row.sentiment_score, 50);
    var turnover = fn(row.turnover_pct, 0);
    var sentimentDelta = fn(row.sentiment_delta, 0);
    var crowding = fn(row.crowding_score, 0);
    var holdingLoss = !!row.holding_loss;
    var daysSinceExit = fn(row.days_since_exit, 999);
    var recentSignal = !!row.recent_signal;
    var existingProfit = fn(row.existing_profit_pct, 0);
    var addOn = !!row.add_on;
    var strongSectorCount = fn(row.strong_sector_count, 0);
    var ma20 = fn(row.ma20, prev);
    var maDeviation = ma20 ? (price / ma20 - 1) * 100 : 0;
    var independentConfirmation = !!row.independent_confirmation;
    var eventRisk = !!row.event_risk;
    var trendBreak = !!row.trend_break;
    var rsReversal = !!row.relative_strength_reversal;
    var sectorFading = !!row.sector_fading;
    var atr = fn(row.atr14, Math.max(price * 0.03, 0.01));
    var closePos = (price - low) / Math.max(high - low, 1e-9);
    var change = (price - prev) / Math.max(prev, 1e-9) * 100;
    var trendOk = price >= prev && closePos >= 0.65;
    var breakout = high ? price >= high * 0.995 : false;
    var high20 = fn(row.high20, high);
    var pivot = fn(row.pivot_price, high20);
    var breakoutVolumeRatio = fn(row.breakout_volume_ratio, amountRatio);
    var distanceFromBreakout = pivot ? (price / pivot - 1) * 100 : 0;
    var atrPct = atr / Math.max(price, 1e-9) * 100;
    var trendConsistency = fn(row.trend_consistency, 1);
    var pivotBreakout = price >= pivot * 0.995
      && breakoutVolumeRatio >= fn(cfg.min_breakout_volume_ratio, 1.2);
    var relativeStrength = fn(row.relative_strength, change);
    var leader = !!row.sector_leader
      || relativeStrength >= fn(row.sector_avg_change_pct, 0) + 2;

    var weights = scoreWeights(cfg);
    var maxDev = fn(cfg.max_ma_deviation_pct, 10);
    var maxDist = fn(cfg.max_distance_from_breakout_pct, 5);
    var maxAr = fn(cfg.max_amount_ratio, 8);

    // 1) 趋势健康度
    // trend_consistency 缺失时给中性 0.5，不能沿用默认 1.0——那等于白送满分
    var consistencyV = has(row, 'trend_consistency') ? ramp(trendConsistency, 0.40, 0.90) : 0.5;
    var maHealth = 1 - ramp(Math.abs(maDeviation), maxDev * 0.5, maxDev);
    var sTrend = 0.32 * (trendOk ? 1 : 0)
      + 0.24 * consistencyV
      + 0.22 * ramp(closePos, 0.35, 0.85)
      + 0.22 * maHealth;
    // 2) 资金动能
    var sFlow = 0.70 * sat(moneyFlow, 4.0) + 0.30 * volumeBand(amountRatio, maxAr);
    // 3) 板块地位
    var sSector = 0.70 * ramp(sectorScore, 45, 95) + 0.30 * (leader ? 1 : 0.15);
    // 4) 突破结构
    var proximity = 1 - Math.min(Math.abs(distanceFromBreakout) / Math.max(maxDist, 1e-9), 1);
    var sSetup = 0.50 * proximity + 0.50 * (pivotBreakout ? 1 : 0);
    // 5) 情绪环境：子项按实际可用字段归一化，缺失不能当利好
    var sentParts = [];
    if (has(row, 'sentiment_score')) sentParts.push([0.45, ramp(sentiment, 40, 85)]);
    if (has(row, 'sentiment_delta')) sentParts.push([0.25, ramp(sentimentDelta, -5, 10)]);
    if (has(row, 'crowding_score')) sentParts.push([0.30, ramp(100 - crowding, 30, 75)]);
    var sentW = sentParts.reduce(function (a, p) { return a + p[0]; }, 0);
    var sSent = sentW
      ? sentParts.reduce(function (a, p) { return a + p[0] * p[1]; }, 0) / sentW
      : 0;
    // 6) 流动性
    var sLiq = 0.60 * turnoverBand(turnover)
      + 0.40 * (1 - ramp(amountRatio, maxAr * 0.8, maxAr * 1.5));
    // 7) 大盘环境
    var sMarket = ramp(marketScore, 35, 70);

    var dims = { trend: sTrend, flow: sFlow, sector: sSector, setup: sSetup,
                 sentiment: sSent, liquidity: sLiq, market: sMarket };

    // 缺失维度从加权里剔除、权重按比例分给其余维度，
    // 否则「没采到数据」会被默认值伪装成「这只股票情绪差」
    var available = { trend: 1, flow: 1, sector: 1, setup: 1, liquidity: 1, market: 1 };
    if (sentParts.length) available.sentiment = 1;
    if (!has(row, 'turnover_pct')) delete available.liquidity;
    var active = {}, totalW = 0;
    Object.keys(available).forEach(function (k) { active[k] = weights[k]; totalW += weights[k]; });
    if (!totalW) totalW = 1;
    var score = 0;
    Object.keys(active).forEach(function (k) { score += active[k] * dims[k]; });
    score = 100 * score / totalW;

    var notes = [];
    if (marketScore < 42) { score *= 0.82; notes.push('市场环境偏弱，整体降权 18%'); }
    else if (marketScore < 55) { score *= 0.93; notes.push('市场环境震荡，适度降权 7%'); }
    if (eventRisk) { score *= 0.75; notes.push('存在事件风险，整体降权 25%'); }
    // 追涨惩罚：当日大涨的股票在结构维度反而拿满分，但它已经没有安全的入场空间
    var changeCap = fn(cfg.max_change_pct, 8);
    var overheat = ramp(change, changeCap * 0.6, changeCap * 1.2);
    if (overheat > 0) {
      score *= 1 - 0.25 * overheat;
      notes.push('当日涨幅 ' + change.toFixed(1) + '%，追涨降权 ' + (25 * overheat).toFixed(0) + '%');
    }
    score = clip(score, 0, 100);

    var entryLow = price * (session === 'midday' ? 0.995 : 0.99);
    var entryHigh = price * (session === 'midday' ? 1.005 : 1.01);
    var stop = Math.max(low - 0.3 * atr, price * 0.94);
    var target = price + Math.max(1.8 * atr, price * 0.06);
    var space = (target / Math.max(price, 1e-9) - 1) * 100;

    var action;
    if (score >= fn(cfg.buy_score_min, 72)
        && marketScore >= fn(cfg.market_score_min_buy, 50)
        && change < fn(cfg.max_change_pct, 8)) action = '条件买入';
    else if (score >= fn(cfg.watch_score_min, 60)) action = '重点观察';
    else action = '放弃';

    var reasons = [];
    function down(text) { action = '重点观察'; if (wantReasons) reasons.push(text); }
    function drop(text) { action = '放弃'; if (wantReasons) reasons.push(text); }

    if (action === '条件买入' && maDeviation > fn(cfg.max_ma_deviation_pct, 10)) {
      down('利弗莫尔原则：远离均线，等待回踩而非追高');
    }
    if (action === '条件买入' && distanceFromBreakout > fn(cfg.max_distance_from_breakout_pct, 5)) {
      down('利弗莫尔原则：突破后涨幅过大，等待回踩');
    }
    if (action === '条件买入' && cfg.require_leader && !leader) {
      down('利弗莫尔原则：优先交易板块领涨股');
    }
    if (action === '条件买入' && fn(cfg.require_independent_confirmation, 1) && !independentConfirmation) {
      down('利弗莫尔原则：不凭消息交易，等待独立数据确认');
    }
    var setupState;
    if (trendBreak || rsReversal || sectorFading) setupState = '趋势破坏';
    else if (pivotBreakout && price > pivot) setupState = '突破确认';
    else if (price >= pivot * 0.98) setupState = '突破前观察';
    else setupState = '等待靠近关键位';
    if (action === '条件买入' && eventRisk) drop('事件风险未排除，停止交易');
    var nextAction = { '等待靠近关键位': '等待，不追价', '突破前观察': '观察突破和量能',
                       '突破确认': '条件买入/持有', '趋势破坏': '减仓或退出' }[setupState] || '观察';
    if (trendBreak || rsReversal || sectorFading) {
      if (action === '条件买入') action = '重点观察';
      if (wantReasons) reasons.push('趋势/相对强度/板块状态出现破坏，优先减仓或退出');
    }
    if (action === '条件买入' && trendConsistency < fn(cfg.min_trend_consistency, 0.4)) {
      down('利弗莫尔原则：趋势不清晰，等待确认');
    }
    if (action === '条件买入' && atrPct > fn(cfg.max_atr_pct, 8)) {
      down('利弗莫尔原则：波动过大，暂不追价');
    }
    if (action === '条件买入' && strongSectorCount < fn(cfg.min_strong_sectors, 1)) {
      down('利弗莫尔原则：等待市场与板块共同确认');
    }
    if (addOn) {
      if (existingProfit > 0 && trendOk && pivotBreakout && action !== '放弃') {
        if (wantReasons) reasons.push('利弗莫尔原则：盈利仓顺势加仓');
      } else {
        if (action !== '放弃') action = '重点观察';
        if (wantReasons) reasons.push('利弗莫尔原则：只向盈利仓加仓，未满足条件');
      }
    }
    if (action === '条件买入' && holdingLoss) drop('利弗莫尔原则：亏损仓禁止补仓摊平');
    if (action === '条件买入' && daysSinceExit < fn(cfg.cooldown_days, 2)) {
      down('刚止损后处于冷却期，等待新机会');
    }
    if (action === '条件买入' && recentSignal && fn(cfg.avoid_repeat_signal, 1)) {
      down('近期已有同向信号，避免过度交易');
    }
    if (action === '条件买入' && sentimentDelta < fn(cfg.min_sentiment_delta, -5)) {
      down('情绪流退潮，资金流信号降级');
    }
    if (action === '条件买入' && crowding >= fn(cfg.max_crowding_score, 90)) {
      down('情绪过度拥挤，禁止追高');
    }
    if (action === '条件买入' && fn(cfg.require_trend_confirmation, 1) && !trendOk) {
      down('利弗莫尔原则：趋势未确认，不逆势交易');
    }
    if (action === '条件买入' && fn(cfg.require_breakout_confirmation, 1) && !pivotBreakout) {
      down('利弗莫尔原则：未完成枢轴突破和量能确认');
    }
    if (action === '条件买入' && change > fn(cfg.max_change_pct, 8)) {
      down('当日涨幅过高，避免追涨');
    }
    if (action === '条件买入' && turnover < fn(cfg.min_turnover_pct, 1)) {
      down('换手率过低，流动性不足');
    }
    if (action === '条件买入' && amountRatio > fn(cfg.max_amount_ratio, 8)) {
      down('成交额异常放大，谨防冲高回落');
    }
    if (action === '条件买入' && sentiment >= fn(cfg.overheat_sentiment, 95)) {
      down('市场情绪过热，避免追高');
    }

    var riskPct = Math.abs((stop / Math.max(price, 1e-9) - 1) * 100);
    var rr = space / Math.max(riskPct, 0.01);
    var riskBudget = fn(cfg.risk_budget_pct, 0.5);
    var maxExposure = fn(cfg.max_exposure, 1.0);
    var positionPct = Math.min(maxExposure, riskBudget / Math.max(riskPct, 0.01));
    if (action === '条件买入' && space < fn(cfg.min_target_space_pct, 0)) {
      down('预期空间低于配置阈值');
    }
    if (action === '条件买入' && rr < fn(cfg.min_risk_reward, 1.5)) {
      down('风险收益比低于配置阈值');
    }
    if (action === '条件买入' && Math.abs((stop / Math.max(price, 1e-9) - 1) * 100) > fn(cfg.max_stop_pct, 100)) {
      down('止损幅度超过配置阈值');
    }

    // 原则通过率（原实现用 endswith('_confirmed') 过滤，漏掉了 *_confirmation，
    // 通过率上限被锁死在 75%；这里显式列出四项）
    var coreFlags = [trendOk, pivotBreakout, leader, independentConfirmation];
    var passRate = coreFlags.filter(Boolean).length / coreFlags.length * 100;
    if (action === '条件买入' && passRate < fn(cfg.min_principle_pass_rate, 50)) {
      down('交易原则通过率不足，等待条件补齐');
    }

    var dimOut = {};
    if (wantReasons) {
      DIM_LABELS.forEach(function (p) {
        if (active[p[0]]) dimOut[p[1]] = [round(active[p[0]] * dims[p[0]] * 100 / totalW, 1),
                                          round(active[p[0]] * 100 / totalW, 0)];
      });
      // 与 build_ranking 的 rows[].reasons 保持同样的构成行，报告里能直接看出分数来源
      reasons.unshift('评分构成 ' + DIM_LABELS.filter(function (p) { return active[p[0]]; })
        .map(function (p) {
          return p[1] + round(active[p[0]] * dims[p[0]] * 100 / totalW, 1) + '/'
            + round(active[p[0]] * 100 / totalW, 0);
        }).join(' '));
      if (trendOk) reasons.push('趋势向上且收盘位于强势区');
      else reasons.push('趋势未确认，不追单');
      if (maDeviation > fn(cfg.max_ma_deviation_pct, 10)) {
        reasons.push('价格偏离20日均线 ' + maDeviation.toFixed(2) + '%');
      }
      if (distanceFromBreakout > fn(cfg.max_distance_from_breakout_pct, 5)) {
        reasons.push('距离突破位过远 ' + distanceFromBreakout.toFixed(2) + '%');
      }
      if (independentConfirmation) reasons.push('独立行情/资金数据已确认');
      if (trendConsistency >= 0.7) reasons.push('趋势一致性 ' + trendConsistency.toFixed(2));
      else if (trendConsistency < 0.4) reasons.push('趋势反复，暂不交易');
      if (atrPct > fn(cfg.max_atr_pct, 8)) reasons.push('波动率过高 ATR ' + atrPct.toFixed(2) + '%');
      if (leader) reasons.push('板块相对强度领先（' + relativeStrength.toFixed(2) + '%）');
      else reasons.push('非板块领涨股，降低优先级');
      if (breakout) reasons.push('接近日内突破位');
      if (amountRatio >= 1.5) reasons.push('成交额温和放大 ' + amountRatio.toFixed(2) + ' 倍');
      if (moneyFlow > 2) reasons.push('资金流强度 ' + moneyFlow.toFixed(2) + '%');
      else if (moneyFlow > 0) reasons.push('资金流转正 ' + moneyFlow.toFixed(2) + '%');
      if (closePos >= 0.8) reasons.push('价格位于日内强势区');
      else if (closePos < 0.35) reasons.push('价格处于日内弱势区');
      if (sectorScore >= 70) reasons.push('所属题材强度 ' + sectorScore.toFixed(1));
      if (2 <= turnover && turnover <= 18) reasons.push('换手率适中 ' + turnover.toFixed(1) + '%');
      reasons.push.apply(reasons, notes);
      reasons.push('风险收益比 ' + rr.toFixed(2));
      reasons.push('建议仓位 ' + (positionPct * 100).toFixed(1) + '%');
    }

    return {
      symbol: String(row.symbol || ''), name: String(row.name || ''),
      score: round(score, 2), action: action,
      buy_price: entryLow.toFixed(2) + '-' + entryHigh.toFixed(2),
      target_price: target.toFixed(2), space_pct: round(space, 2),
      stop_price: round(stop, 2), stop_pct: round((stop / Math.max(price, 1e-9) - 1) * 100, 2),
      setup_state: setupState, next_action: nextAction,
      risk_reward: round(rr, 2), reasons: reasons, dims: dimOut,
      journal: { trend_confirmed: trendOk, breakout_confirmed: pivotBreakout,
                 leader_confirmed: leader, independent_confirmation: independentConfirmation,
                 event_risk: eventRisk, risk_reward: round(rr, 2), next_action: nextAction,
                 principle_pass_rate: round(passRate, 2) }
    };
  }

  // ---------- 全市场重排 ----------
  // opts: {market_score, session, config, top, limit}
  // 返回 {ranking, stats, dims, flow_source}
  function rank(rows, opts) {
    opts = opts || {};
    var cfg = opts.config || {};
    var session = opts.session || 'close';
    var market = fn(opts.market_score, 58.35);
    var limit = opts.limit || 100;

    var en = enrich(rows, fn(cfg.strong_sector_score_min, 70));
    rows.forEach(function (r) {
      r.sector_score = en.scores[String(r.sector || '未分类')];
      r.strong_sector_count = en.strong;
      // 独立确认：信号全部来自行情数据即满足利弗莫尔「不凭消息交易」
      if (r.independent_confirmation === undefined) {
        r.independent_confirmation = String(r.signal_source || 'market_data') === 'market_data';
      }
    });

    var plans = rows.map(function (r) { return scoreOne(r, market, session, cfg, false); });
    plans.sort(function (a, b) { return b.score - a.score; });

    var scores = plans.map(function (p) { return p.score; });
    var n = scores.length || 1;
    var sorted = scores.slice().sort(function (a, b) { return a - b; });
    var sum = scores.reduce(function (a, b) { return a + b; }, 0);
    var mean = sum / n;
    // 总体标准差（对应 statistics.pstdev，不是样本标准差）
    var variance = scores.reduce(function (a, s) { return a + (s - mean) * (s - mean); }, 0) / n;
    var stats = {
      count: plans.length,
      max: scores.length ? round(Math.max.apply(null, scores), 2) : 0,
      min: scores.length ? round(Math.min.apply(null, scores), 2) : 0,
      median: scores.length ? round(sorted[Math.floor(n / 2)], 2) : 0,
      mean: round(mean, 2),
      stdev: round(Math.sqrt(variance), 2),
      ge72: scores.filter(function (s) { return s >= fn(cfg.buy_score_min, 72); }).length,
      ge60: scores.filter(function (s) { return s >= 60; }).length,
      actions: {}, sectors: Object.keys(en.scores).length
    };
    plans.forEach(function (p) { stats.actions[p.action] = (stats.actions[p.action] || 0) + 1; });

    var bySymbol = {};
    rows.forEach(function (r) { bySymbol[String(r.symbol)] = r; });

    var out = plans.slice(0, Math.max(limit, 100)).map(function (p, i) {
      var r = bySymbol[p.symbol] || {};
      // 只有进榜的那些才补完整理由，全市场 5000 只都拼会明显拖慢
      var full = i < limit ? scoreOne(r, market, session, cfg, true) : p;
      return {
        rank: i + 1, symbol: p.symbol, name: p.name, score: p.score,
        action: full.action, setup_state: full.setup_state, next_action: full.next_action,
        buy_price: full.buy_price, space_pct: full.space_pct, stop_pct: full.stop_pct,
        change_pct: r.change_pct, money_flow_pct: r.money_flow_pct,
        amount_ratio: r.amount_ratio, turnover_pct: r.turnover_pct,
        sector: r.sector, sector_score: r.sector_score,
        risk_reward: full.risk_reward, dims: full.dims
      };
    });
    return { ranking: out, stats: stats, sectors: en.scores };
  }

  // 入选时间（对应 selection_time.annotate）：首次进榜的秒级时刻，连续在榜保持不变；
  // 掉出榜超过宽限期再回来才重新计时。时间一律按北京时间，
  // 直接用 toISOString 会写成 UTC，榜单上就会显示成「03:08」而实际是 11:08。
  var GRACE_MS = 120000, NEW_MS = 180000;
  function bjStamp(ms) {
    var d = new Date(ms + 8 * 3600000);
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getUTCFullYear() + '-' + p(d.getUTCMonth() + 1) + '-' + p(d.getUTCDate())
      + 'T' + p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ':' + p(d.getUTCSeconds());
  }
  function makeSelectionBook(store) {
    var KEY = 'fm_selection_state';
    function load() {
      try {
        var raw = store && store.getItem(KEY);
        var obj = raw ? JSON.parse(raw) : null;
        return obj && typeof obj === 'object' ? obj : {};
      } catch (e) { return {}; }
    }
    function save(s) { try { store && store.setItem(KEY, JSON.stringify(s)); } catch (e) { /* 隐私模式/配额 */ } }
    return function annotate(rows, keep) {
      var now = Date.now();
      var today = bjStamp(now).slice(0, 10);
      var state = load();
      if (state.date !== today) state = { date: today, rank: {} };
      var book = state.rank || (state.rank = {});
      var onList = {};
      rows.slice(0, keep || 100).forEach(function (r) { if (r.symbol) onList[r.symbol] = 1; });
      Object.keys(onList).forEach(function (sym) {
        if (book[sym] && book[sym].at) book[sym].seen = now;
        else book[sym] = { at: bjStamp(now), atMs: now, seen: now };
      });
      Object.keys(book).forEach(function (sym) {
        if (onList[sym]) return;
        if (now - (book[sym].seen || 0) > GRACE_MS) delete book[sym];
      });
      save(state);
      rows.forEach(function (r) {
        var rec = book[r.symbol];
        if (!rec || !onList[r.symbol]) { delete r.selected_at; delete r.selected_new; return; }
        r.selected_at = rec.at;
        // 用首次进榜的毫秒数判定「新入榜」，不能用 rec.seen——那个每次刷新都在变
        r.selected_new = (now - (rec.atMs || now)) <= NEW_MS;
      });
      return rows;
    };
  }

  var API = {
    fn: fn, has: has, sat: sat, ramp: ramp, volumeBand: volumeBand, turnoverBand: turnoverBand,
    scoreWeights: scoreWeights, enrich: enrich, elapsedRatio: elapsedRatio, phase: phase,
    bjClock: bjClock, applyQuotes: applyQuotes, scoreOne: scoreOne, rank: rank,
    makeSelectionBook: makeSelectionBook
  };
  root.FMScore = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : globalThis);
