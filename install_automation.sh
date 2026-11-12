#!/bin/bash
# 安装 / 卸载 全自动研究流水线的 launchd 定时任务
#
#   ./install_automation.sh            安装（看板常驻 + 午盘 11:35、收盘 15:10，周一至周五）
#   ./install_automation.sh uninstall  卸载
#   ./install_automation.sh status     查看状态
#   ./install_automation.sh run close  立刻手动跑一次（等价于定时触发）
#
# 说明：原先项目里的 com.ths.research.sessions.plist 从未安装到 ~/Library/LaunchAgents，
# 所以定时任务一直没生效。本脚本同时负责清理那个历史遗留配置。
# 看板服务用 KeepAlive 常驻，挂掉由 launchd 自动拉起，不再依赖手动启动的临时进程。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGDIR="$ROOT/logs"
ACTION="${1:-install}"

# ---- 选一个 >=3.10 的解释器：/usr/bin/python3 是 3.9，跑不了本项目的类型标注 ----
pick_python() {
  local candidates=(
    /opt/homebrew/bin/python3
    /usr/local/bin/python3
    "$HOME/miniforge3/bin/python3"
    python3
  )
  for py in "${candidates[@]}"; do
    command -v "$py" >/dev/null 2>&1 || continue
    if "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      command -v "$py"
      return 0
    fi
  done
  echo "找不到 Python 3.10+，请先安装" >&2
  return 1
}

PYTHON="$(pick_python)"
mkdir -p "$AGENTS" "$LOGDIR"

make_plist() {
  local session="$1" hour="$2" minute="$3"
  local label="com.ths.research.$session"
  local target="$AGENTS/$label.plist"
  {
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ROOT/auto_pipeline.py</string>
    <string>--session</string><string>$session</string>
    <string>--workers</string><string>32</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartCalendarInterval</key>
  <array>
PLIST
    for wd in 1 2 3 4 5; do
      cat <<ITEM
    <dict>
      <key>Weekday</key><integer>$wd</integer>
      <key>Hour</key><integer>$hour</integer>
      <key>Minute</key><integer>$minute</integer>
    </dict>
ITEM
    done
    cat <<PLIST
  </array>
  <key>StandardOutPath</key><string>$LOGDIR/launchd_$session.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/launchd_$session.err.log</string>
  <key>RunAtLoad</key><false/>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
</dict>
</plist>
PLIST
  } > "$target"
  echo "  已写入 $target"
}

make_dashboard_plist() {
  local label="com.ths.research.dashboard"
  local target="$AGENTS/$label.plist"
  cat > "$target" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ROOT/dashboard_server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOGDIR/dashboard.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/dashboard.err.log</string>
</dict>
</plist>
PLIST
  echo "  已写入 $target"
}

unload_all() {
  for s in midday close dashboard sessions; do
    local f="$AGENTS/com.ths.research.$s.plist"
    if [ -f "$f" ]; then
      launchctl bootout "gui/$(id -u)" "$f" >/dev/null 2>&1 || \
        launchctl unload "$f" >/dev/null 2>&1 || true
    fi
  done
}

case "$ACTION" in
  install)
    echo "解释器: $PYTHON"
    echo "项目目录: $ROOT"
    unload_all
    # 清掉历史遗留的、从未生效的那个配置
    rm -f "$AGENTS/com.ths.research.sessions.plist"
    make_plist midday 11 35
    make_plist close  15 10
    make_dashboard_plist
    # 常驻服务要独占 8787，先停掉手动启动的临时进程，否则 launchd 会一直重启失败
    if lsof -nP -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then
      echo "  停止 8787 上手动启动的看板进程，改由 launchd 接管"
      lsof -ti:8787 | xargs kill 2>/dev/null || true
      sleep 1
    fi
    for s in midday close dashboard; do
      launchctl bootstrap "gui/$(id -u)" "$AGENTS/com.ths.research.$s.plist" 2>/dev/null || \
        launchctl load "$AGENTS/com.ths.research.$s.plist"
    done
    echo
    echo "已安装：看板常驻（KeepAlive，挂掉自动拉起）/ 午盘 11:35 / 收盘 15:10（周一至周五）"
    echo "看板地址：http://127.0.0.1:8787"
    echo "非交易日会自动跳过，不产生报告也不告警。"
    echo "运行日志：$LOGDIR/auto_\$(date +%Y%m%d).log"
    ;;
  uninstall)
    unload_all
    rm -f "$AGENTS/com.ths.research.midday.plist" \
          "$AGENTS/com.ths.research.close.plist" \
          "$AGENTS/com.ths.research.dashboard.plist"
    echo "已卸载全部 THS 定时任务与常驻看板"
    ;;
  status)
    echo "=== launchd 任务 ==="
    launchctl list | grep -i "com.ths.research" || echo "  （未加载任何任务）"
    echo
    echo "=== 看板服务 ==="
    curl -s -m 3 -o /dev/null -w "  http://127.0.0.1:8787  http=%{http_code}\n" \
      http://127.0.0.1:8787/ || echo "  （未响应，需执行 install）"
    echo
    echo "=== 最近运行摘要 ==="
    for s in midday close; do
      if [ -f "$ROOT/auto_output/latest_$s.json" ]; then
        echo "--- $s ---"
        "$PYTHON" - "$ROOT/auto_output/latest_$s.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print("  时间:", d.get("finished_at"), "| 耗时:", d.get("elapsed_sec"), "s",
      "| 全部成功:", d.get("all_ok"), "| 跳过:", d.get("skipped", False))
if d.get("steps"):
    for st in d["steps"]:
        print(f"    {'OK ' if st['ok'] else 'FAIL'} {st['step']:22} {st['elapsed_sec']}s")
PY
      else
        echo "  （尚未运行过）"
      fi
    done
    ;;
  run)
    SESSION="${2:-close}"
    echo "手动触发 $SESSION ..."
    cd "$ROOT" && "$PYTHON" auto_pipeline.py --session "$SESSION" --workers 32
    ;;
  *)
    echo "用法: $0 [install|uninstall|status|run <midday|close>]" >&2
    exit 2
    ;;
esac
