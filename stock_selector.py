"""Midday/close stock candidate scorer for the H5 research pipeline.

Input is a JSON list of normalized stock snapshots. The module produces
conditional plans (entry, target space, stop and reasons); it never places
orders.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class Plan:
    symbol: str
    name: str
    action: str
    score: float
    buy_price: str
    target_price: str
    space_pct: float
    stop_price: float
    stop_pct: float
    reasons: list[str]
    invalid_conditions: list[str]
    setup_state: str
    next_action: str
    journal: dict[str, Any]
    pivot_price: float
    high20: float
    pullback_price: float


def _f(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = float(row.get(key, default))
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


# ===== 评分原语 =====
# 旧评分是 score=35 起步逐项加减，加分项合计可达 +100 以上，结果全市场 Top10 全部被
# clip 在 100.0，排序彻底失去区分度。以下三个函数用于「加权饱和」评分：
# 每个维度先算出 0~1 的强度，再按权重加权求和。因为每维强度天然有上界，
# 单项再强也无法拉爆总分；要拿高分必须多个维度同时强。

def _sat(x: float, half: float) -> float:
    """指数饱和，用于无上界指标（资金流强度、板块分等）。

    x=half 时返回 0.5，x→∞ 渐近 1。极端值只能逼近上限，不会线性刷爆。
    """
    if half <= 0:
        return 0.0
    return 1.0 - 2.0 ** (-max(0.0, x) / half)


def _ramp(x: float, lo: float, hi: float) -> float:
    """分段线性归一，用于本身上下界明确的指标（收盘位置、趋势一致性等）。

    <=lo 归零，>=hi 满分，中间线性过渡。上界明确所以不需要指数饱和。
    """
    if hi <= lo:
        return 1.0 if x >= hi else 0.0
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


def _volume_band(amount_ratio: float, max_ratio: float) -> float:
    """量能最优区间判定：1.2~3 倍为健康放量，过低无量能，过高多为冲高博弈。"""
    if amount_ratio <= 0:
        return 0.0
    if amount_ratio < 1.2:
        return 0.8 * _ramp(amount_ratio, 0.5, 1.2)
    if amount_ratio <= 3.0:
        return 1.0
    return max(0.25, 1.0 - 0.75 * (amount_ratio - 3.0) / max(max_ratio - 3.0, 1e-9))


def _turnover_band(turnover: float) -> float:
    """换手率最优区间：2%~18%。过低流动性不足，过高多为短线炒作接力。"""
    if turnover <= 0:
        return 0.0
    if turnover < 2.0:
        return _ramp(turnover, 0.5, 2.0)
    if turnover <= 18.0:
        return 1.0
    return max(0.2, 1.0 - (turnover - 18.0) / 30.0)


DEFAULT_SCORE_WEIGHTS: dict[str, float] = {
    "trend": 22.0,      # 趋势健康度
    "flow": 20.0,       # 资金动能
    "sector": 18.0,     # 板块地位
    "setup": 14.0,      # 突破结构
    "sentiment": 12.0,  # 情绪环境
    "liquidity": 8.0,   # 流动性
    "market": 6.0,      # 大盘环境
}


def _score_weights(config: dict[str, Any]) -> dict[str, float]:
    """读取维度权重并归一化到总和 100，保证总分区间仍是 0~100、阈值语义不变。"""
    weights = dict(DEFAULT_SCORE_WEIGHTS)
    for key, value in (config.get("score_weights") or {}).items():
        if key in weights:
            try:
                weights[key] = float(value)
            except (TypeError, ValueError):
                pass
    total = sum(weights.values())
    if total <= 0:
        return dict(DEFAULT_SCORE_WEIGHTS)
    return {key: value * 100.0 / total for key, value in weights.items()}


def score_stock(row: dict[str, Any], market_score: float, session: str, config: dict[str, Any] | None = None) -> Plan:
    config=config or {}
    price = _f(row, "price")
    prev = _f(row, "prev_close", price)
    high = _f(row, "high", price)
    low = _f(row, "low", price)
    amount_ratio = _f(row, "amount_ratio", 1.0)
    money_flow = _f(row, "money_flow_pct")
    sector = _f(row, "sector_score")
    sentiment = _f(row, "sentiment_score", 50)
    turnover = _f(row, "turnover_pct")
    sentiment_delta=_f(row,'sentiment_delta',0)
    crowding=_f(row,'crowding_score',0)
    holding_loss=bool(row.get('holding_loss',False))
    days_since_exit=_f(row,'days_since_exit',999)
    recent_signal=bool(row.get('recent_signal',False))
    existing_profit_pct=_f(row,'existing_profit_pct',0)
    add_on=bool(row.get('add_on',False))
    strong_sector_count=_f(row,'strong_sector_count',0)
    ma20=_f(row,'ma20',prev)
    ma_deviation=(price/ma20-1)*100 if ma20 else 0
    independent_confirmation=bool(row.get('independent_confirmation',False))
    signal_source=str(row.get('signal_source','market_data'))
    event_risk=bool(row.get('event_risk',False))
    trend_break=bool(row.get('trend_break',False))
    relative_strength_reversal=bool(row.get('relative_strength_reversal',False))
    sector_fading=bool(row.get('sector_fading',False))
    atr = _f(row, "atr14", max(price * 0.03, 0.01))
    close_pos = (price - low) / max(high - low, 1e-9)
    change = (price - prev) / max(prev, 1e-9) * 100
    trend_ok = price >= prev and close_pos >= 0.65
    breakout = price >= high * 0.995 if high else False
    high20=_f(row,'high20',high)
    pivot=_f(row,'pivot_price',high20)
    breakout_volume_ratio=_f(row,'breakout_volume_ratio',amount_ratio)
    distance_from_breakout=(price/pivot-1)*100 if pivot else 0
    atr_pct=atr/max(price,1e-9)*100
    trend_consistency=_f(row,'trend_consistency',1)
    pivot_breakout = price >= pivot * 0.995 and breakout_volume_ratio >= float(config.get('min_breakout_volume_ratio',1.2))
    relative_strength = _f(row, "relative_strength", change)
    leader = bool(row.get("sector_leader", False)) or relative_strength >= _f(row, "sector_avg_change_pct", 0) + 2
    # ===== 评分：加权饱和曲线 =====
    # 七个维度各自算出 0~1 的强度，再按权重加权求和。每维强度天然有上界，
    # 单项极度强势也只能拿满本维度权重，无法像旧版那样把总分线性推到 100 之上。
    reasons: list[str] = []
    weights = _score_weights(config)
    max_dev = float(config.get('max_ma_deviation_pct', 10))
    max_dist = float(config.get('max_distance_from_breakout_pct', 5))
    max_ar = float(config.get('max_amount_ratio', 8))

    # 1) 趋势健康度：方向 + 连续性 + 日内位置 + 均线偏离健康度
    # trend_consistency 缺失时给中性 0.5，不能沿用默认值 1.0——那等于白送满分
    consistency_v = _ramp(trend_consistency, 0.40, 0.90) if 'trend_consistency' in row else 0.5
    ma_health = 1.0 - _ramp(abs(ma_deviation), max_dev * 0.5, max_dev)
    s_trend = (0.32 * (1.0 if trend_ok else 0.0)
               + 0.24 * consistency_v
               + 0.22 * _ramp(close_pos, 0.35, 0.85)
               + 0.22 * ma_health)

    # 2) 资金动能：主力净流入走饱和曲线，量能按最优区间判定
    s_flow = 0.70 * _sat(money_flow, 4.0) + 0.30 * _volume_band(amount_ratio, max_ar)

    # 3) 板块地位：题材强度 + 是否领涨
    s_sector = 0.70 * _ramp(sector, 45, 95) + 0.30 * (1.0 if leader else 0.15)

    # 4) 突破结构：距关键位的接近度 + 是否完成放量突破
    proximity = 1.0 - min(abs(distance_from_breakout) / max(max_dist, 1e-9), 1.0)
    s_setup = 0.50 * proximity + 0.50 * (1.0 if pivot_breakout else 0.0)

    # 5) 情绪环境：水平 + 变化方向 + 拥挤度（拥挤反向计）
    # 子项按实际可用字段归一化，避免「只有情绪分、没有拥挤度」时把缺失的 crowding
    # 默认成 0 再算成「完全不拥挤」拿满分——那同样是拿缺失数据当利好。
    sent_parts: list[tuple[float, float]] = []
    if 'sentiment_score' in row:
        sent_parts.append((0.45, _ramp(sentiment, 40, 85)))
    if 'sentiment_delta' in row:
        sent_parts.append((0.25, _ramp(sentiment_delta, -5, 10)))
    if 'crowding_score' in row:
        sent_parts.append((0.30, _ramp(100.0 - crowding, 30, 75)))
    sent_w = sum(w for w, _ in sent_parts)
    s_sent = sum(w * v for w, v in sent_parts) / sent_w if sent_w else 0.0

    # 6) 流动性：换手适中 + 量能不过度异常
    s_liq = (0.60 * _turnover_band(turnover)
             + 0.40 * (1.0 - _ramp(amount_ratio, max_ar * 0.8, max_ar * 1.5)))

    # 7) 大盘环境
    s_market = _ramp(market_score, 35, 70)

    dims = {'trend': s_trend, 'flow': s_flow, 'sector': s_sector, 'setup': s_setup,
            'sentiment': s_sent, 'liquidity': s_liq, 'market': s_market}

    # 字段可用性：enrich() 目前只产出 sector_score，情绪类字段可能整段缺失。
    # 缺失维度必须从加权里剔除、权重按比例分给其余维度，否则「没采到数据」
    # 会被默认值伪装成「这只股票情绪差」——那是错误归因，不是评分。
    available = {'trend', 'flow', 'sector', 'setup', 'liquidity', 'market'}
    if sent_parts:
        available.add('sentiment')
    if 'turnover_pct' not in row:
        available.discard('liquidity')
    active = {k: weights[k] for k in available}
    total_w = sum(active.values()) or 1.0
    score = 100.0 * sum(active[k] * dims[k] for k in active) / total_w

    # 环境乘数：市场规模性风险或个股事件风险时整体打折。
    # 用乘数而非硬扣分，保证结果始终落在 0~100，也不会让个别项压倒其他维度。
    notes: list[str] = []
    if market_score < 42:
        score *= 0.82; notes.append('市场环境偏弱，整体降权 18%')
    elif market_score < 55:
        score *= 0.93; notes.append('市场环境震荡，适度降权 7%')
    if event_risk:
        score *= 0.75; notes.append('存在事件风险，整体降权 25%')
    # 追涨惩罚（连续衰减，无断崖）：当日大涨的股票「距枢轴 0%」，在结构维度反而拿满分，
    # 但它已经没有安全的入场空间。不在评分里压下去的话，Top10 会全是涨停股，
    # 随后又被 max_change_pct 全部拦截——排序结果和可执行性就脱节了。
    change_cap = float(config.get('max_change_pct', 8))
    overheat = _ramp(change, change_cap * 0.6, change_cap * 1.2)
    if overheat > 0:
        score *= 1.0 - 0.25 * overheat
        notes.append(f'当日涨幅 {change:.1f}%，追涨降权 {25 * overheat:.0f}%')
    score = max(0.0, min(100.0, score))

    if trend_ok:
        reasons.append("趋势向上且收盘位于强势区")
    else:
        reasons.append("趋势未确认，不追单")
    if ma_deviation > max_dev:
        reasons.append(f"价格偏离20日均线 {ma_deviation:.2f}%")
    if distance_from_breakout > max_dist:
        reasons.append(f"距离突破位过远 {distance_from_breakout:.2f}%")
    if independent_confirmation:
        reasons.append("独立行情/资金数据已确认")
    if event_risk:
        reasons.append("公告/事件风险未排除")
    if trend_consistency >= 0.7:
        reasons.append(f"趋势一致性 {trend_consistency:.2f}")
    elif trend_consistency < 0.4:
        reasons.append("趋势反复，暂不交易")
    if atr_pct > float(config.get('max_atr_pct',8)):
        reasons.append(f"波动率过高 ATR {atr_pct:.2f}%")
    if leader:
        reasons.append(f"板块相对强度领先（{relative_strength:.2f}%）")
    else:
        reasons.append("非板块领涨股，降低优先级")
    if breakout:
        reasons.append("接近日内突破位")
    if amount_ratio >= 1.5:
        reasons.append(f"成交额温和放大 {amount_ratio:.2f} 倍")
    if money_flow > 2:
        reasons.append(f"资金流强度 {money_flow:.2f}%")
    elif money_flow > 0:
        reasons.append(f"资金流转正 {money_flow:.2f}%")
    if close_pos >= 0.8:
        reasons.append("价格位于日内强势区")
    elif close_pos < 0.35:
        reasons.append("价格处于日内弱势区")
    if sector >= 70:
        reasons.append(f"所属题材强度 {sector:.1f}")
    if sentiment >= 65:
        reasons.append(f"情绪分 {sentiment:.1f}")
    if sentiment_delta > 5:
        reasons.append(f"情绪升温 {sentiment_delta:.1f}")
    elif sentiment_delta < -5:
        reasons.append(f"情绪退潮 {sentiment_delta:.1f}")
    if crowding >= 80:
        reasons.append("情绪拥挤，谨防一致性高潮")
    elif 'crowding_score' in row and crowding <= 30 and money_flow > 0 and sector >= 60:
        # 必须显式判断字段存在：crowding 缺失时默认 0 会被误读成"情绪不拥挤"
        reasons.append("资金流与题材共振但情绪尚未拥挤")
    if 2 <= turnover <= 18:
        reasons.append(f"换手率适中 {turnover:.1f}%")
    reasons.extend(notes)
    # 维度明细写进理由，报告里能直接看出分数由哪些维度构成
    # 权重按实际参与计算的维度归一化后展示，避免"权重加起来不等于100"的误读
    dim_labels = (("trend", "趋势"), ("flow", "资金"), ("sector", "板块"), ("setup", "结构"),
                  ("sentiment", "情绪"), ("liquidity", "流动"), ("market", "大盘"))
    reasons.append("评分构成 " + " ".join(
        f"{label}{active[key] * dims[key] * 100.0 / total_w:.1f}"
        f"/{active[key] * 100.0 / total_w:.0f}"
        for key, label in dim_labels if key in active
    ))

    # Conditional plan: no chase if already extended; use pullback/reclaim trigger.
    entry_low = price * (0.995 if session == "midday" else 0.99)
    entry_high = price * (1.005 if session == "midday" else 1.01)
    stop = max(low - 0.3 * atr, price * 0.94)
    target = price + max(1.8 * atr, price * 0.06)
    space = (target / max(price, 1e-9) - 1) * 100
    action = "观察"
    if score >= float(config.get('buy_score_min',72)) and market_score >= float(config.get('market_score_min_buy',50)) and change < float(config.get('max_change_pct',8)):
        action = "条件买入"
    elif score >= float(config.get('watch_score_min',60)):
        action = "重点观察"
    else:
        action = "放弃"
    if action == "条件买入" and ma_deviation > float(config.get('max_ma_deviation_pct',10)):
        action = "重点观察"; reasons.append("利弗莫尔原则：远离均线，等待回踩而非追高")
    if action == "条件买入" and distance_from_breakout > float(config.get('max_distance_from_breakout_pct',5)):
        action = "重点观察"; reasons.append("利弗莫尔原则：突破后涨幅过大，等待回踩")
    # 「只做板块领涨股」原本只有配置（require_leader: true）和四处错挂的提示语，
    # 真正的判定从来没写。提示语挂在 ATR / 强势板块 / 突破位 / 成交额四个分支下，
    # 会出现「波动过大被降级，理由却写优先领涨股」这种自相矛盾（实测该股 leader=True）。
    # 这里补成独立闸门，并把那四句错挂的提示删掉。
    if action == "条件买入" and config.get('require_leader', False) and not leader:
        action = "重点观察"; reasons.append("利弗莫尔原则：优先交易板块领涨股")
    if action == "条件买入" and config.get('require_independent_confirmation', True) and not independent_confirmation:
        action = "重点观察"; reasons.append("利弗莫尔原则：不凭消息交易，等待独立数据确认")
    if trend_break or relative_strength_reversal or sector_fading:
        setup_state='趋势破坏'
    elif pivot_breakout and price > pivot:
        setup_state='突破确认'
    elif price >= pivot*0.98:
        setup_state='突破前观察'
    else:
        setup_state='等待靠近关键位'
    if action == "条件买入" and event_risk:
        action = "放弃"; reasons.append("事件风险未排除，停止交易")
    next_action={'等待靠近关键位':'等待，不追价','突破前观察':'观察突破和量能','突破确认':'条件买入/持有','趋势破坏':'减仓或退出'}.get(setup_state,'观察')
    if trend_break or relative_strength_reversal or sector_fading:
        if action == "条件买入": action = "重点观察"
        reasons.append("趋势/相对强度/板块状态出现破坏，优先减仓或退出")
    if action == "条件买入" and trend_consistency < float(config.get('min_trend_consistency',0.4)):
        action = "重点观察"; reasons.append("利弗莫尔原则：趋势不清晰，等待确认")
    if action == "条件买入" and atr_pct > float(config.get('max_atr_pct',8)):
        action = "重点观察"; reasons.append("利弗莫尔原则：波动过大，暂不追价")
    if action == "条件买入" and strong_sector_count < float(config.get('min_strong_sectors',1)):
        action = "重点观察"; reasons.append("利弗莫尔原则：等待市场与板块共同确认")
    if add_on:
        if existing_profit_pct > 0 and trend_ok and pivot_breakout and action != "放弃":
            reasons.append(f"利弗莫尔原则：盈利仓顺势加仓，已有盈利 {existing_profit_pct:.2f}%")
        else:
            action = "重点观察" if action != "放弃" else action
            reasons.append("利弗莫尔原则：只向盈利仓加仓，未满足条件")
    if action == "条件买入" and holding_loss:
        action = "放弃"; reasons.append("利弗莫尔原则：亏损仓禁止补仓摊平")
    if action == "条件买入" and days_since_exit < float(config.get('cooldown_days',2)):
        action = "重点观察"; reasons.append("刚止损后处于冷却期，等待新机会")
    if action == "条件买入" and recent_signal and config.get('avoid_repeat_signal',True):
        action = "重点观察"; reasons.append("近期已有同向信号，避免过度交易")
    if action == "条件买入" and sentiment_delta < float(config.get('min_sentiment_delta',-5)):
        action = "重点观察"; reasons.append("情绪流退潮，资金流信号降级")
    if action == "条件买入" and crowding >= float(config.get('max_crowding_score',90)):
        action = "重点观察"; reasons.append("情绪过度拥挤，禁止追高")
    if action == "条件买入" and config.get('require_trend_confirmation', True) and not trend_ok:
        action = "重点观察"; reasons.append("利弗莫尔原则：趋势未确认，不逆势交易")
    if action == "条件买入" and config.get('require_breakout_confirmation', True) and not pivot_breakout:
        action = "重点观察"; reasons.append("利弗莫尔原则：未完成枢轴突破和量能确认")
    if action == "条件买入" and change > float(config.get('max_change_pct', 8)):
        action = "重点观察"; reasons.append("当日涨幅过高，避免追涨")
    if action == "条件买入" and turnover < float(config.get('min_turnover_pct', 1)):
        action = "重点观察"; reasons.append("换手率过低，流动性不足")
    if action == "条件买入" and amount_ratio > float(config.get('max_amount_ratio', 8)):
        action = "重点观察"; reasons.append("成交额异常放大，谨防冲高回落")
    if action == "条件买入" and sentiment >= float(config.get('overheat_sentiment',95)):
        action = "重点观察"; reasons.append("市场情绪过热，避免追高")
    risk_pct=abs((stop / max(price,1e-9)-1)*100)
    rr=space/max(risk_pct,0.01)
    risk_budget=float(config.get('risk_budget_pct',0.5))
    max_exposure=float(config.get('max_exposure',1.0))
    position_pct=min(max_exposure, risk_budget/max(risk_pct,0.01))
    if add_on and existing_profit_pct > 0 and trend_ok and pivot_breakout:
        pyramid_level=2 if existing_profit_pct >= float(config.get('second_add_profit_pct',5)) else 1
        position_pct=min(max_exposure, position_pct*(1.0+pyramid_level*0.25))
        reasons.append(f"顺势加仓级别 {pyramid_level}")
    else:
        pyramid_level=0
    reasons.append(f"风险收益比 {rr:.2f}")
    reasons.append(f"建议仓位 {position_pct*100:.1f}%")
    if action == "条件买入" and space < float(config.get('min_target_space_pct', 0)):
        action = "重点观察"; reasons.append("预期空间低于配置阈值")
    if action == "条件买入" and rr < float(config.get('min_risk_reward',1.5)):
        action = "重点观察"; reasons.append("风险收益比低于配置阈值")
    if action == "条件买入" and abs((stop / max(price, 1e-9) - 1) * 100) > float(config.get('max_stop_pct', 100)):
        action = "重点观察"; reasons.append("止损幅度超过配置阈值")
    journal={
        'trend_confirmed': trend_ok, 'breakout_confirmed': pivot_breakout,
        'leader_confirmed': leader, 'independent_confirmation': independent_confirmation,
        'event_risk': event_risk,
        # 情绪类字段若未采集，记 null 而不是默认值——日记里不该出现看起来像真实数据的假分
        'sentiment_score': sentiment if 'sentiment_score' in row else None,
        'sentiment_delta': sentiment_delta if 'sentiment_delta' in row else None,
        'crowding_score': crowding if 'crowding_score' in row else None,
        'risk_reward': round(rr,2), 'next_action': next_action,
    }
    unmet=[]
    if not trend_ok: unmet.append('趋势未确认')
    if not pivot_breakout: unmet.append('枢轴突破/量能未确认')
    if not leader: unmet.append('非板块领涨')
    if not independent_confirmation: unmet.append('缺少独立行情资金确认')
    if event_risk: unmet.append('存在事件风险')
    if sentiment_delta < -5: unmet.append('情绪退潮')
    if crowding >= 90: unmet.append('情绪拥挤')
    if turnover < float(config.get('min_turnover_pct',1)): unmet.append('换手率过低')
    if abs((stop / max(price,1e-9)-1)*100) > float(config.get('max_stop_pct',100)): unmet.append('止损幅度过宽')
    if rr < float(config.get('min_risk_reward',1.5)): unmet.append('风险收益比不足')
    if position_pct >= max_exposure and max_exposure < 1: unmet.append('总仓位受市场状态限制')
    # 原实现用 k.endswith('_confirmed') 过滤，但 independent_confirmation 结尾是
    # '_confirmation'，从未被计入分子，分母却仍是 4 —— 通过率上限被锁死在 75%。
    # 这里显式列出四项，避免命名再被误伤。
    core_flags = ('trend_confirmed', 'breakout_confirmed', 'leader_confirmed', 'independent_confirmation')
    journal['principle_pass_rate'] = round(
        sum(bool(journal[k]) for k in core_flags) / len(core_flags) * 100, 2)
    journal['unmet_count']=len(unmet)
    core_unmet=[x for x in unmet if x in ('趋势未确认','枢轴突破/量能未确认','非板块领涨','缺少独立行情资金确认','存在事件风险')]
    journal['core_unmet_conditions']=core_unmet
    journal['core_conditions_passed']=len(core_unmet)==0
    if action == "条件买入" and journal['principle_pass_rate'] < float(config.get('min_principle_pass_rate',50)):
        action = "重点观察"; reasons.append("交易原则通过率不足，等待条件补齐")
    invalid = ["跌破止损价", "所属题材强度跌破50", "资金流连续转负", "亏损后禁止向下摊平", "盈利仓位不满足趋势延续则退出", "趋势破坏或板块退潮时减仓退出"]
    return Plan(
        symbol=str(row.get("symbol", "")), name=str(row.get("name", "")), action=action,
        score=round(score, 2), buy_price=f"{entry_low:.2f}-{entry_high:.2f}",
        target_price=f"{target:.2f}", space_pct=round(space, 2), stop_price=round(stop, 2),
        stop_pct=round((stop / max(price, 1e-9) - 1) * 100, 2), reasons=reasons,
        invalid_conditions=invalid, setup_state=setup_state, next_action=next_action, journal=journal, pivot_price=round(pivot,2), high20=round(high20,2), pullback_price=round(pivot*1.005,2),
    )


def select(rows: list[dict[str, Any]], market_score: float, session: str, limit: int = 10, config: dict[str, Any] | None = None) -> list[Plan]:
    plans = [score_stock(row, market_score, session, config) for row in rows if _f(row, "price") > 0]
    # 可执行标的必须置顶。纯按分数取 Top-N 会出现「看得见的都不能买、能买的看不见」：
    # 实测唯一一只「条件买入」排第 18，被 Top10 直接截掉，而榜上 10 只全是「重点观察」。
    # 先按动作分组（条件买入优先），组内再按分数降序。
    plans.sort(key=lambda x: (x.action == "条件买入", x.score), reverse=True)
    return plans[:limit]


def render(plans: list[Plan], market_score: float, session: str) -> str:
    lines = [f"# {session} 个股测试报告", "", f"市场分：{market_score:.2f}", ""]
    for i, p in enumerate(plans, 1):
        lines += [f"## {i}. {p.symbol} {p.name}", f"- 结论：**{p.action}**", f"- 评分：{p.score:.2f}", f"- 交易阶段：**{p.setup_state}**", f"- 下一动作：**{p.next_action}**", f"- 交易日记：`{json.dumps(p.journal, ensure_ascii=False)}`", f"- 买入区间：`{p.buy_price}`", f"- 目标价：`{p.target_price}`（空间 {p.space_pct:.2f}%）", f"- 止损价：`{p.stop_price:.2f}`（{p.stop_pct:.2f}%）", f"- 关键枢轴价：`{p.pivot_price:.2f}`", f"- 20日高点：`{p.high20:.2f}`", f"- 回踩确认价：`{p.pullback_price:.2f}`", "- 买入理由："]
        lines += [f"  - {x}" for x in p.reasons] or ["  - 暂无足够强度理由"]
        lines += ["- 失效条件：" + "；".join(p.invalid_conditions), ""]
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="normalized stock snapshot JSON")
    ap.add_argument("--market-score", type=float, required=True)
    ap.add_argument("--session", choices=["midday", "close"], default="close")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--output", default="stock_report.md")
    ap.add_argument("--config", type=Path, help="strategy config JSON")
    args = ap.parse_args()
    rows = json.loads(Path(args.input).read_text(encoding="utf-8"))
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    plans = select(rows, args.market_score, args.session, args.limit, config)
    Path(args.output).write_text(render(plans, args.market_score, args.session), encoding="utf-8")
    print(json.dumps([asdict(x) for x in plans], ensure_ascii=False, indent=2))
