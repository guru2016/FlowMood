#!/bin/bash
# 在 VPS 上执行：解压 -> 用户级 systemd 常驻 -> 定时任务 -> 冒烟验证
# 不需要 root：全部装在 ~/.config/systemd/user，前提 Linger=yes（脚本会检查）。
set -euo pipefail

APP_DIR="${1:-$HOME/ths_research}"
PY="$(command -v python3)"
TARBALL="/tmp/ths_app.tar.gz"
UNIT_DIR="$HOME/.config/systemd/user"

echo "=== 0. 环境预检 ==="
echo "用户: $(id -un) | 主机: $(hostname) | 目录: $APP_DIR"
"$PY" -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10+"; print("Python:", sys.version.split()[0])'
command -v curl >/dev/null || { echo "缺少 curl（采集与实时行情依赖它）"; exit 1; }
systemctl --user list-units >/dev/null 2>&1 || { echo "用户级 systemd 不可用"; exit 1; }
echo "systemd: $(systemctl --version | head -1)"
echo "时区: $(date '+%Y-%m-%d %H:%M:%S %Z')"
if loginctl show-user "$(id -un)" 2>/dev/null | grep -q 'Linger=yes'; then
  echo "Linger: yes（注销/重启后服务仍会运行）"
else
  echo "Linger: no，尝试开启（可能需要管理员）"
  loginctl enable-linger "$(id -un)" 2>/dev/null || echo "  警告：开启失败，注销后服务可能停止"
fi
if (ss -ltn 2>/dev/null | grep -q ':8787 '); then
  if systemctl --user is-active ths-dashboard.service >/dev/null 2>&1; then
    echo "8787 由本项目 ths-dashboard 占用（重部署），将先停后起"
  else
    echo "错误：8787 被其他服务占用，中止以免撞掉现有服务"; exit 1
  fi
fi
[ -f "$TARBALL" ] || { echo "找不到 $TARBALL"; exit 1; }
echo

echo "=== 1. 解压 ==="
# 重部署时先停自己的服务，避免解压覆盖正在被读取的代码文件
systemctl --user stop ths-dashboard.service >/dev/null 2>&1 || true
if [ -d "$APP_DIR" ]; then
  BAK="${APP_DIR}.bak.$(date +%Y%m%d_%H%M%S)"
  echo "备份旧目录 -> $BAK"
  mv "$APP_DIR" "$BAK"
fi
mkdir -p "$APP_DIR"
tar -xzf "$TARBALL" -C "$APP_DIR"
mkdir -p "$APP_DIR/logs" "$APP_DIR/auto_output" "$APP_DIR/frontend_output" "$UNIT_DIR"
echo "文件数: $(find "$APP_DIR" -type f | wc -l | tr -d ' ')"
echo

echo "=== 2. 生成 systemd 单元 ==="
cat > "$UNIT_DIR/ths-dashboard.service" <<UNIT
[Unit]
Description=THS Research Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=TZ=Asia/Shanghai
Environment=THS_HOST=127.0.0.1
Environment=THS_PORT=8787
ExecStart=$PY $APP_DIR/dashboard_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
UNIT

cat > "$UNIT_DIR/ths-pipeline@.service" <<UNIT
[Unit]
Description=THS Research Pipeline (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
Environment=TZ=Asia/Shanghai
ExecStart=$PY $APP_DIR/auto_pipeline.py --session %i --workers 32
UNIT

make_timer() {
  local name="$1" hhmm="$2"
  cat > "$UNIT_DIR/ths-$name.timer" <<UNIT
[Unit]
Description=THS $name pipeline timer

[Timer]
OnCalendar=Mon..Fri *-*-* $hhmm:00 Asia/Shanghai
Persistent=false
Unit=ths-pipeline@$name.service

[Install]
WantedBy=timers.target
UNIT
}
make_timer midday 11:35
make_timer close  15:10
echo "已写入 4 个单元: dashboard.service / pipeline@.service / midday.timer / close.timer"
echo

echo "=== 3. 启动 ==="
systemctl --user daemon-reload
systemctl --user enable --now ths-dashboard.service >/dev/null
systemctl --user restart ths-dashboard.service   # 确保加载本次部署的新代码
systemctl --user enable --now ths-midday.timer ths-close.timer >/dev/null
sleep 2
systemctl --user is-active ths-dashboard.service | sed 's/^/dashboard: /'
echo

echo "=== 4. 冒烟测试（用打包快照跑一次收盘链路，不重新采集） ==="
cd "$APP_DIR"
if timeout 240 "$PY" auto_pipeline.py --session close --skip-collect > /tmp/ths_smoke.log 2>&1; then
  echo "流水线: OK"
  tail -3 /tmp/ths_smoke.log
else
  echo "流水线有异常（服务仍已启动，详情见 /tmp/ths_smoke.log）"
  tail -12 /tmp/ths_smoke.log
fi
echo

echo "=== 5. 验证 ==="
curl -s -m 10 http://127.0.0.1:8787/api/status -o /tmp/ths_status.json || true
"$PY" - <<'PY'
import json
try:
    d = json.load(open('/tmp/ths_status.json'))
    r = d.get('report') or {}
    print('  看板 /api/status: HTTP OK')
    print('  市场分:', r.get('market_score'), '| 候选:', len(r.get('plans') or []))
    print('  最新报告:', d.get('report_path'))
except Exception as exc:
    print('  看板接口未就绪:', exc)
PY
echo "  定时任务:"
systemctl --user --no-pager list-timers 2>/dev/null | grep -E 'ths-(midday|close)' || echo "    （未列出，请检查 daemon-reload）"
echo
echo "部署完成。看板仅监听 127.0.0.1:8787（安全默认）。"
echo "本机隧道: ssh -p <ssh_port> -N -L 8787:127.0.0.1:8787 <user>@<vps_host>"
