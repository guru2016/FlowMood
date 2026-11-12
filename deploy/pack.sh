#!/bin/bash
# 生成部署包。只打包运行必需的文件，排除 apktool(1.2G)、历史报告、缓存与日志。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist/ths_app.tar.gz}"
mkdir -p "$(dirname "$OUT")"
cd "$ROOT"

FILES=()
for f in *.py *.md *.json; do
  [ -f "$f" ] && FILES+=("$f")
done
FILES+=(web/dashboard.html dynamic/h5_api deploy)

# 看板首屏要用的初始数据（不带全部历史报告，避免包体膨胀）
for f in auto_output/latest_close.json auto_output/latest_midday.json \
         auto_output/latest_ranking.json auto_output/research.sqlite3; do
  [ -f "$f" ] && FILES+=("$f")
done
for s in close midday; do
  d="$(ls -dt "auto_output/$s"/*/ 2>/dev/null | head -1 || true)"
  [ -n "$d" ] && FILES+=("${d%/}")
done

# macOS bsdtar 默认写入 com.apple.provenance 扩展头，Linux GNU tar 不认识会刷警告。
# --no-xattrs + COPYFILE_DISABLE 让包在两端都干净。
COPYFILE_DISABLE=1 tar -czf "$OUT" --no-xattrs \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' \
  --exclude='dist' --exclude='*.log' \
  "${FILES[@]}"

echo "打包完成: $OUT"
echo "  体积 $(du -h "$OUT" | cut -f1) / 文件数 $(tar -tzf "$OUT" | wc -l | tr -d ' ')"
