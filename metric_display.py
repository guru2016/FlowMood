#!/usr/bin/env python3
"""核心指标的「人话化」层。

流水线抓下来的 H5 大盘指标是 ``{date, net_inflow, szzz, ...}`` 这样的原始字段。
以前报告正文直接把 ``sjdp_xxx`` 代码名 + 整串 JSON 打印出来，看板上是一堆代码。
本模块负责：

1. 用接口自带的中文名（``lines[].name``）把字段翻译成中文标签；
2. 把每个指标整理成「中文名 / 主数值 / 辅助字段 / 数据日期」的结构化卡片数据；
3. 把报告正文里的核心指标段与交易日记 JSON 换成可读的中文结果行；
4. 给本地服务与 GitHub Pages 提供同一个 ``normalize_report()`` 入口。

字段名与含义以同花顺 H5 接口返回为准，不自行猜测单位，因此卡片只显示数值本身。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

# (指标中文名, 主展示字段, 卡片里列出的字段顺序)
SPEC: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "sjdp_market_capital": ("大盘资金", "net_inflow", ("net_inflow", "szzz")),
    "sjdp_north_capital": ("北向资金", "net_inflow", ("net_purchase", "net_inflow", "szzz")),
    "sjdp_temperature_hs": ("市场情绪", "temperature", ("temperature",)),
    "sjdp_rzrq": ("融资融券", "jlr", ("lrye", "jlr", "szzz")),
    "sjdp_new_investors": ("新增开户", "invest", ("invest", "szzz")),
    "sjdp_market_financing": ("市场融资", "bond", ("ipo", "add", "bond", "szzz")),
    "sjdp_lift_the_ban": ("限售解禁", "lift_the_ban", ("lift_the_ban", "szzz")),
    "sjdp_increase_or_decrease": ("增减持", "zjc", ("zjc", "szzz")),
    "sjdp_new_fund_size": ("新增基金", "new_fund_size", ("new_fund_size", "szzz")),
    "sjdp_reverse_repurchase": ("逆回购", "jtf", ("jtf", "jhg", "szzz")),
    "sjdp_dollar_rmb": ("美元人民币", "dollar_rmb", ("dollar_rmb", "szzz")),
}

METRIC_LABELS: dict[str, str] = {k: v[0] for k, v in SPEC.items()}

# 字段中文名：取自 H5 接口 data.lines[].name，未自定义单位
FIELD_LABELS: dict[str, str] = {
    "date": "数据日期",
    "net_inflow": "净流入",
    "net_purchase": "净买额",
    "szzz": "上证指数",
    "temperature": "市场温度",
    "lrye": "两融余额",
    "jlr": "净流入",
    "invest": "新增开户数",
    "ipo": "IPO 募资",
    "add": "增发",
    "bond": "债券融资",
    "lift_the_ban": "解禁规模",
    "zjc": "增减持",
    "new_fund_size": "新增基金规模",
    "jtf": "净投放",
    "jhg": "净回购",
    "dollar_rmb": "美元/人民币",
}

# 只有「有方向」的资金字段套用红涨绿跌；存量/规模/汇率/指数保持中性
SIGNED_FIELDS = {"net_inflow", "net_purchase", "jlr", "zjc", "jtf", "jhg"}
STALE_DAYS = 30


def _num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _tone(field: str, value: Any) -> str:
    n = _num(value)
    if field == "temperature" and n is not None:
        return "up" if n >= 70 else "down" if n <= 30 else "flat"
    if field in SIGNED_FIELDS and n is not None:
        return "up" if n > 0 else "down" if n < 0 else "flat"
    return "flat"


def _date_info(raw: Any) -> dict:
    """20261111 -> 11-11；202610 -> 2026-10；未来日期/过期数据单独标记。"""
    s = str(raw or "").strip()
    full, short, parsed, future, monthly = s, s, None, False, False
    if re.fullmatch(r"\d{8}", s):
        parsed = datetime.strptime(s, "%Y%m%d").date()
        full = parsed.strftime("%Y-%m-%d")
        short = parsed.strftime("%m-%d")
    elif re.fullmatch(r"\d{6}", s):
        full = f"{s[:4]}-{s[4:]}"
        short = full
        monthly = True
        parsed = date(int(s[:4]), int(s[4:]), 1)
    today = date.today()
    if parsed and parsed > today:
        future = True
    # 月度数据（新增开户/新发基金）按月更新，别按日频误判成过期
    limit = 95 if monthly else STALE_DAYS
    stale = bool(parsed and (today - parsed).days > limit)
    return {"date": s, "date_full": full, "date_text": short,
            "stale": stale, "future": future}


def _display_value(field: str, value: Any) -> str:
    """保留接口原始精度并加千分位；整数不带多余的小数零。"""
    if value is None or value == "":
        return "—"
    n = _num(value)
    if n is None:
        return str(value)
    raw = str(value).strip()
    text = f"{n:,.2f}" if "." in raw else f"{n:,.0f}"
    if field in SIGNED_FIELDS and n > 0:
        text = "+" + text
    return text


def build_metric(key: str, raw: dict | None) -> dict | None:
    """把一条原始指标转成前端卡片用的结构，里面不含任何代码名或 JSON 串。"""
    if key not in SPEC or not isinstance(raw, dict) or not raw:
        return None
    label, main_field, fields = SPEC[key]
    items = []
    for field in fields:
        if field not in raw:
            continue
        items.append({
            "key": field,
            "label": FIELD_LABELS.get(field, field),
            "value": _display_value(field, raw.get(field)),
            "tone": _tone(field, raw.get(field)),
            "empty": raw.get(field) is None or raw.get(field) == "",
        })

    # 主字段可能整期为 null（例如市场融资）；退而取第一个有值的非指数字段。
    # 指数只是背景参考，绝不拿来冒充该指标本身的结果。
    main_key = main_field
    chosen = next((i for i in items if i["key"] == main_key and not i["empty"]), None)
    if chosen is None:
        chosen = next((i for i in items if i["key"] != "szzz" and not i["empty"]), None)
        main_key = chosen["key"] if chosen else main_field
    no_data = chosen is None
    main_value = chosen["value"] if chosen else "暂无数据"
    main_tone = chosen["tone"] if chosen else "flat"
    info = _date_info(raw.get("date"))
    hint = ""
    if no_data:
        hint = "本期未披露"
    elif info["future"]:
        hint = "预告"
    elif info["stale"]:
        hint = "历史数据"
    elif main_key == "temperature":
        t = _num(raw.get("temperature"))
        hint = "偏热" if t is not None and t >= 70 else "偏冷" if t is not None and t <= 30 else "中性"
    return {
        "label": label,
        "main_label": "本期披露" if no_data else FIELD_LABELS.get(main_key, "数值"),
        "main_value": main_value,
        "main_tone": main_tone,
        "main_key": main_key,
        "no_data": no_data,
        "hint": hint,
        "items": [i for i in items if i["key"] != main_key and not i["empty"]],
        **info,
    }


def metrics_from_raw(raw: dict[str, dict] | None) -> list[dict]:
    """按 SPEC 的固定顺序整理新报告里附带的原始指标。"""
    out = []
    for key in SPEC:
        m = build_metric(key, (raw or {}).get(key))
        if m:
            out.append(m)
    return out


_METRIC_LINE = re.compile(r"^- (.+?)（(sjdp_[a-z_]+)）：`(\{.*\})`\s*$")
_JOURNAL_LINE = re.compile(r"^- 交易日记：`(\{.*\})`\s*$")


def parse_report_metrics(markdown: str) -> list[dict]:
    """兼容历史报告：从旧版「核心指标」段里把 JSON 解析回结构化数据。"""
    found: dict[str, dict] = {}
    for line in (markdown or "").splitlines():
        m = _METRIC_LINE.match(line.strip())
        if not m:
            continue
        try:
            found[m.group(2)] = json.loads(m.group(3))
        except (ValueError, TypeError):
            continue
    return metrics_from_raw(found)


def humanize_journal(journal: Any) -> str:
    """交易日记 JSON -> 一句中文结果。"""
    if not isinstance(journal, dict) or not journal:
        return "交易审计：暂无明细"
    flags = (("trend_confirmed", "趋势"), ("breakout_confirmed", "突破"),
             ("leader_confirmed", "龙头"), ("independent_confirmation", "独立确认"))
    passed = [name for key, name in flags if journal.get(key) is True]
    parts = [f"核心条件 {len(passed)}/{len(flags)} 项"]
    if passed:
        parts.append("已确认 " + "、".join(passed))
    rate = _num(journal.get("principle_pass_rate"))
    if rate is not None:
        parts.append(f"原则通过率 {rate:g}%")
    rr = _num(journal.get("risk_reward"))
    if rr is not None:
        parts.append(f"风险收益比 {rr:g}")
    if journal.get("next_action"):
        parts.append(f"下一动作 {journal['next_action']}")
    try:
        unmet = int(journal.get("unmet_count") or 0)
        if unmet:
            parts.append(f"未满足 {unmet} 项")
    except (TypeError, ValueError):
        pass
    return "交易审计：" + " · ".join(parts)


def format_metric_line(key: str, raw: dict) -> str:
    """报告正文用：只写中文结果，不写代码名和 JSON。"""
    m = build_metric(key, raw)
    if not m:
        return "- 暂无指标数据"
    bits = [f"{m['main_label']} {m['main_value']}"]
    bits += [f"{i['label']} {i['value']}" for i in m["items"] if not i["empty"]]
    return f"- {m['label']}：{' · '.join(bits)} · {m['date_full']}" \
           + ("（历史数据）" if m["stale"] else "")


def clean_report_markdown(markdown: str) -> str:
    """移除核心指标段（改由卡片区呈现），并把交易日记 JSON 换成中文结果。"""
    out: list[str] = []
    skipping = False
    for line in (markdown or "").splitlines():
        s = line.strip()
        if s.startswith("## "):
            skipping = s[3:].strip() == "核心指标"
            if skipping:
                continue
        if skipping:
            continue
        j = _JOURNAL_LINE.match(s)
        if j:
            try:
                journal = json.loads(j.group(1))
            except (ValueError, TypeError):
                journal = {}
            out.append("- " + humanize_journal(journal))
            continue
        out.append(line)
    return "\n".join(out).strip() + "\n"


def normalize_report(report: dict) -> dict:
    """/api/reports 与 build_static.py 共用：补齐 metrics，清理正文。幂等。"""
    r = dict(report or {})
    md = r.get("markdown") or ""
    raw = r.get("metrics")
    if isinstance(raw, list) and raw:
        metrics = raw
    elif isinstance(raw, dict) and raw:
        metrics = metrics_from_raw(raw)
    else:
        metrics = parse_report_metrics(md)
    r["metrics"] = metrics
    r["markdown"] = clean_report_markdown(md)
    return r
