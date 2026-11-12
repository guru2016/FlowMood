#!/usr/bin/env python3
"""把本机真实定稿同步进 sample/，供 GitHub Pages 使用。

云端 Actions 经常取不到东财全市场列表（出口被限流），构建时就会回退到
sample/ 里的快照。这个脚本把本机跑出来的完整定稿搬进 sample/，让线上
看板与本地一致；只搬「已通过质量闸门」的排名与当日报告，体积几百 KB。

用法：
    python3 sync_sample.py            # 同步排名 + 报告 + 状态
    python3 sync_sample.py --dry-run  # 只看会写什么
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "sample"


def _dump(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _rel(p: str | None) -> str | None:
    """把绝对路径压成项目相对路径，公开仓库里不留本机目录结构。"""
    if not p:
        return p
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except Exception:  # noqa: BLE001
        return p if not str(p).startswith(("/", "C:", "/Users", "/home")) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from build_static import _live_reports
    from metric_display import normalize_report

    src = ROOT / "auto_output" / "latest_ranking.json"
    if not src.exists():
        print("没有 auto_output/latest_ranking.json，先跑一次流水线")
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))
    quality = data.get("snapshot_quality") or {}
    if not quality.get("ok", True):
        print(f"拒绝同步：定稿快照不完整（{quality.get('reason')}）")
        return 1

    rows = data.get("ranking") or []
    print(f"排名 {data.get('generated_at')} · {len(rows)} 行 · "
          f"最高 {data.get('stats', {}).get('max')} · flow_source={data.get('flow_source')}")

    reports = _live_reports()
    for k, v in reports.items():
        print(f"报告 {k} · {v.get('generated_at')} · 市场分 {v.get('market_score')} · "
              f"候选 {len(v.get('plans') or [])}")

    status: dict = {}
    try:
        from dashboard_server import status as status_fn
        raw = status_fn()
        rep = raw.get("report") or {}
        status = {
            "report": normalize_report(rep) if rep else {},
            "report_path": _rel(raw.get("report_path")),
            "alerts": raw.get("alerts") or {},
            "auto": {k: {"stamp": (v or {}).get("stamp"),
                         "finished_at": (v or {}).get("finished_at"),
                         "all_ok": (v or {}).get("all_ok")}
                     for k, v in (raw.get("auto") or {}).items()},
        }
    except Exception as exc:  # noqa: BLE001
        print(f"状态快照跳过：{exc}")

    if args.dry_run:
        print("dry-run：不写文件")
        return 0

    if not SAMPLE.exists():
        try:
            SAMPLE.mkdir(parents=True)
        except FileExistsError:   # 沙箱 shim 会把 exist_ok 判成 EEXIST，忽略即可
            pass
    _dump(SAMPLE / "latest_ranking.json", data)
    _dump(SAMPLE / "reports.json", {"reports": reports})
    if status:
        _dump(SAMPLE / "status.json", status)
    total = sum(p.stat().st_size for p in SAMPLE.glob("*.json"))
    print(f"已写入 sample/（共 {total / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
