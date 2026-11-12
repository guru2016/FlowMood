#!/bin/bash
# 一键部署到 VPS：打包 -> scp -> 远端安装
#
#   ./deploy/deploy.sh [user@host] [ssh_port] [远端目录]
#   也可用环境变量 THS_DEPLOY_TARGET / THS_DEPLOY_PORT / THS_DEPLOY_DIR 指定。
#   仓库内默认值是占位符——公开仓库不写真实主机信息。
#
# 依赖本机已配置 SSH 免密（公钥登录）。不需要 root、不需要密码。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-${THS_DEPLOY_TARGET:-deploy@your-vps.example.com}}"
PORT="${2:-${THS_DEPLOY_PORT:-22}}"
APP_DIR="${3:-${THS_DEPLOY_DIR:-/home/deploy/ths_research}}"
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)

echo "目标: $TARGET:$PORT  ->  $APP_DIR"
echo "--- 1/4 打包 ---"
"$ROOT/deploy/pack.sh" "$ROOT/dist/ths_app.tar.gz"

echo "--- 2/4 上传 ---"
scp -P "$PORT" "${SSH_OPTS[@]}" \
  "$ROOT/dist/ths_app.tar.gz" "$ROOT/deploy/remote_setup.sh" \
  "$TARGET:/tmp/"

echo "--- 3/4 远端安装 ---"
ssh -p "$PORT" "${SSH_OPTS[@]}" "$TARGET" \
  "bash /tmp/remote_setup.sh '$APP_DIR'"

echo "--- 4/4 完成 ---"
echo "看板在 VPS 内部: http://127.0.0.1:8787"
echo "本机访问请建隧道: ssh -p $PORT -N -L 8787:127.0.0.1:8787 $TARGET"
echo "                 然后打开 http://127.0.0.1:8787"
