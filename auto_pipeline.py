#!/usr/bin/env python3
"""全自动流水线调度器 —— 一次调用跑完整个研究链路，无人值守。

【为什么不用 run_all.py】

  run_all.py 的 --auto-market-score 实际是从 dynamic/h5_api/ 读 APK 扒包时留下的
  静态样本（见 run_all.py 第 11 行），算出来的市场分永远停在扒包那天，是"假自动"。
  本模块直接调 daily_runner.run()，它内部实时抓 H5 大盘资金与市场温度，
  市场分随行情真实变化。run_all.py 保持原样不动，不影响既有用法。

【链路】

  1. auto_collect      全市场采集            -> normalized_snapshots.json
  2. daily_runner      实时 H5 + 市场分 + 板块评分 + 个股选股 + 入库 + 报告
  3. performance_tracker  计划入库
  4. performance_daily    当日表现日报
  5. rolling_windows      5 日 / 20 日滚动收益回撤
  6. report_monitor       与上一次对比，异常与候选变化告警
  7. ranking              全市场评分排名，写 latest_ranking.json 供前端读取
  8. manifest             运行摘要 + 失败原因，供健康检查与前端读取

用法：
    python auto_pipeline.py --session close
    python auto_pipeline.py --session midday --skip-collect   # 用已有快照
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 交易日历 / 盘中折算 / 会话时段全部依赖本地时间。VPS 多为 UTC，强制对齐北京时区。
os.environ["TZ"] = os.environ.get("THS_TZ", "Asia/Shanghai")
if hasattr(time, "tzset"):
    time.tzset()

LOG_DIR = ROOT / "logs"
OUT_ROOT = ROOT / "auto_output"


def log(msg: str, logfile: Path | None = None) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        with logfile.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def notify(title: str, message: str, sound: str = "Glass") -> None:
    """告警：macOS 弹通知中心；Linux/VPS 写日志并可选推 webhook。

    失败一律静默——通知绝不能阻断流水线。THS_WEBHOOK_URL 支持企业微信/钉钉/
    Server酱等任意接受 {"text": "..."} 或纯文本的 POST 端点。
    """
    if sys.platform == "darwin":
        try:
            script = (f'display notification {json.dumps(message)} '
                      f'with title {json.dumps(title)} sound name "{sound}"')
            subprocess.run(["osascript", "-e", script], check=False,
                           capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {title} | {message}\n"
        with (LOG_DIR / "notify.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
        webhook = os.environ.get("THS_WEBHOOK_URL", "").strip()
        if webhook:
            import urllib.request
            body = json.dumps({"text": f"{title}\n{message}"}, ensure_ascii=False).encode()
            req = urllib.request.Request(webhook, data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5).close()
    except Exception:  # noqa: BLE001
        pass


def sh(cmd: list[str], logfile: Path, timeout: int = 900) -> tuple[bool, str]:
    """执行子脚本，输出进日志，返回 (成功?, 输出尾部)。"""
    try:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.strip().splitlines()[-30:]:
            log(f"    {line}", logfile)
        return proc.returncode == 0, out
    except subprocess.TimeoutExpired:
        log(f"    超时 {timeout}s: {' '.join(cmd)}", logfile)
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001
        log(f"    异常: {exc}", logfile)
        return False, str(exc)


def latest_report(out_dir: Path, session: str) -> Path | None:
    found = sorted(out_dir.glob(f"stock_report_{session}_*.json"),
                   key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def run_once(session: str = "close", skip_collect: bool = False,
             workers: int = 16, db: Path | None = None,
             config: Path | None = None) -> dict:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logfile = LOG_DIR / f"auto_{datetime.now():%Y%m%d}.log"
    out_dir = OUT_ROOT / session / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    db = db or (OUT_ROOT / "research.sqlite3")
    config = config or (ROOT / "strategy_config.json")
    snapshot = ROOT / "normalized_snapshots.json"

    t0 = time.time()
    steps: list[dict] = []
    log(f"===== 开始 {session} 会话 =====", logfile)

    # 非交易日直接优雅退出：让脚本自己判断，就不必维护节假日日历表
    if not skip_collect:
        from auto_collect import check_trading_day
        trading, last_day = check_trading_day()
        if not trading:
            log(f"非交易日（上证最新日K={last_day or '读取失败'}），今日不产生报告", logfile)
            manifest = {"session": session, "stamp": stamp, "skipped": True,
                        "reason": f"non_trading_day:{last_day}",
                        "finished_at": datetime.now().isoformat(timespec="seconds"),
                        "elapsed_sec": round(time.time() - t0, 1), "all_ok": True}
            (OUT_ROOT / f"latest_{session}.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            return manifest

    def step(name: str, fn) -> bool:
        log(f"[{name}] 开始", logfile)
        s = time.time()
        try:
            ok, detail = fn()
        except SystemExit as exc:  # 子模块用 SystemExit 报致命错，不能让 it 冲穿调度
            ok, detail = False, f"SystemExit: {exc}"
            log(f"    {detail}", logfile)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
            log(f"    {traceback.format_exc().strip().splitlines()[-1]}", logfile)
        steps.append({"step": name, "ok": ok,
                      "elapsed_sec": round(time.time() - s, 1),
                      "detail": (detail or "")[-500:]})
        log(f"[{name}] {'成功' if ok else '失败'} {time.time() - s:.1f}s", logfile)
        return ok

    # 1. 全市场采集
    if skip_collect:
        if not snapshot.exists():
            raise SystemExit("--skip-collect 但 normalized_snapshots.json 不存在")
        steps.append({"step": "collect", "ok": True, "elapsed_sec": 0, "detail": "skipped"})
        log("[collect] 跳过，复用现有快照", logfile)
    else:
        sys.path.insert(0, str(ROOT))
        from auto_collect import collect as auto_collect

        def _collect():
            auto_collect(workers=workers, session=session, output=snapshot)
            return True, "ok"
        if not step("collect", _collect):
            raise SystemExit("采集失败，终止本次运行（不产出半截报告）")

    # 2. 实时 H5 + 市场分 + 板块评分 + 选股 + 入库 + 报告
    from daily_runner import run as daily_run

    report_path: Path | None = None

    def _selector():
        nonlocal report_path
        report_path = daily_run(out_dir, db, None, snapshot, session,
                                ROOT / "dynamic/h5_api",
                                json.loads(config.read_text(encoding="utf-8"))
                                if config.exists() else {})
        return True, str(report_path)
    step("selector", _selector)

    # 3. 计划入库
    plan_json = latest_report(out_dir, session)
    if plan_json:
        step("performance_tracker", lambda: sh(
            [sys.executable, "performance_tracker.py", "--db", str(db),
             "--plan-json", str(plan_json)], logfile))

    # 4/5. 表现统计
    step("performance_daily", lambda: sh(
        [sys.executable, "performance_daily.py", "--db", str(db),
         "--output", str(out_dir / "performance_daily.csv")], logfile))
    step("rolling_windows", lambda: sh(
        [sys.executable, "rolling_windows.py", "--db", str(db),
         "--output", str(out_dir / "rolling_windows.json")], logfile))

    # 6. 异常告警：和上一次同会话报告对比
    prev_runs = sorted([p for p in (OUT_ROOT / session).glob("*/stock_report_*.json")
                        if p.parent != out_dir], key=lambda p: p.stat().st_mtime)
    if plan_json and prev_runs:
        step("report_monitor", lambda: sh(
            [sys.executable, "report_monitor.py", "--current", str(plan_json),
             "--previous", str(prev_runs[-1]), "--output",
             str(out_dir / "alerts.json")], logfile))

    # 7. 全市场评分排名：前端「排名」面板直接消费 latest_ranking.json
    from auto_rank import build_ranking

    def _ranking():
        market = None
        if plan_json:
            try:
                market = float(json.loads(plan_json.read_text(encoding="utf-8"))["market_score"])
            except Exception:  # noqa: BLE001
                market = None
        res = build_ranking(snapshot=snapshot, config=config, market_score=market,
                            top=100, tag=stamp)
        s = res["stats"]
        return True, (f"样本{s['count']} 最高{s['max']:.2f} ≥72分{s['ge72']}只 "
                      f"动作{s['actions']}")
    step("ranking", _ranking)

    # 8. 运行摘要
    manifest = {
        "session": session,
        "stamp": stamp,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_sec": round(time.time() - t0, 1),
        "snapshot": str(snapshot.relative_to(ROOT)),
        "report": str(report_path.relative_to(ROOT)) if report_path else None,
        "db": str(db),
        "steps": steps,
        "all_ok": all(s["ok"] for s in steps),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_ROOT / f"latest_{session}.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [s["step"] for s in steps if not s["ok"]]
    if failed:
        log(f"===== 结束（有失败：{','.join(failed)}）耗时 {manifest['elapsed_sec']}s =====", logfile)
        notify("THS 研究流水线异常", f"{session} 失败环节：{', '.join(failed)}", "Basso")
    else:
        log(f"===== 结束（全部成功）耗时 {manifest['elapsed_sec']}s =====", logfile)
        notify("THS 研究流水线完成", f"{session} 报告已生成，耗时 {manifest['elapsed_sec']}s")

    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="全自动研究流水线")
    ap.add_argument("--session", choices=["midday", "close"], default="close")
    ap.add_argument("--skip-collect", action="store_true", help="复用已有快照，不重新采集")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--config", type=Path)
    args = ap.parse_args()
    manifest = run_once(session=args.session, skip_collect=args.skip_collect,
                        workers=args.workers, db=args.db, config=args.config)
    sys.exit(0 if manifest["all_ok"] else 1)


if __name__ == "__main__":
    main()
