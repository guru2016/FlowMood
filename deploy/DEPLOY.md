# VPS 部署说明

> 仓库里不含任何真实主机信息。目标地址、SSH 端口、远端目录都通过**命令行参数**
> 或 `THS_DEPLOY_*` 环境变量传入；本机已配置 SSH 公钥免密即可，不需要 root、不需要密码。

## 配置目标机器

```bash
# 方式一：环境变量（推荐，可写进本机 shell 配置，不入库）
export THS_DEPLOY_TARGET='user@your-vps.example.com'
export THS_DEPLOY_PORT='22'
export THS_DEPLOY_DIR='/home/user/ths_research'

# 方式二：位置参数
./deploy/deploy.sh user@your-vps.example.com 22 /home/user/ths_research
```

**全程不需要 root、不需要密码**：服务装在用户级 systemd，需确认 `loginctl enable-linger $USER`，
注销和重启后才会自动拉起。

## 一键部署

```bash
cd /path/to/ths_apk_analysis
./deploy/deploy.sh
```

脚本做四件事：本地打包 → scp 上传 → 远端安装 → 冒烟验证。
打包内容：全部 `.py`、配置 `json`、`web/dashboard.html`、`dynamic/h5_api/`、
最近一次报告与 `latest_*.json`。**排除** `apktool/`（1.2G）、历史报告、缓存、日志。

## 部署后的结构

| 项 | 值 |
|---|---|
| 应用目录 | `$THS_DEPLOY_DIR`，默认 `/home/deploy/ths_research` |
| 解释器 | `/usr/bin/python3`（3.12+，项目纯标准库，无 pip 依赖） |
| 看板 | `127.0.0.1:8787`，`ths-dashboard.service`，`Restart=always` |
| 午盘 | `ths-midday.timer` → 周一至周五 11:35 |
| 收盘 | `ths-close.timer` → 周一至周五 15:10 |
| 时区 | 单元里强制 `TZ=Asia/Shanghai`，timer 用带时区的 `OnCalendar` |
| 日志 | `~/ths_research/logs/`、`journalctl --user -u ths-dashboard` |

时区不能错：全项目的交易时段判断、盘中量比折算、定时任务都依赖本地时间。

## 访问方式

看板**默认只监听 127.0.0.1**，不会暴露到公网。本机建隧道后打开
`http://127.0.0.1:8787`：

```bash
ssh -p "${THS_DEPLOY_PORT:-22}" -N -L 8787:127.0.0.1:8787 "$THS_DEPLOY_TARGET"
```

**不要直接把 8787 改成 0.0.0.0 暴露公网。** `/api/run` 没有任何鉴权，
公网可达意味着任何人都能触发流水线、读取全部报告。若确需公网访问，
应先加反向代理 + Basic Auth，并单独禁止外部 `POST /api/run`。

## 运维

```bash
ssh -p "${THS_DEPLOY_PORT:-22}" "$THS_DEPLOY_TARGET"

systemctl --user status ths-dashboard          # 看板状态
systemctl --user restart ths-dashboard        # 重启看板
systemctl --user list-timers | grep ths       # 两次定时
journalctl --user -u ths-dashboard -f         # 实时日志
systemctl --user start ths-pipeline@close     # 手动跑一次收盘
tail -f ~/ths_research/logs/auto_$(date +%Y%m%d).log
```

更新代码：本机改完再执行一次 `./deploy/deploy.sh` 即可。脚本会把旧目录
备份成 `ths_research.bak.<时间戳>`，不会直接删除。

停止并卸载：

```bash
systemctl --user disable --now ths-dashboard ths-midday.timer ths-close.timer
rm -f ~/.config/systemd/user/ths-{dashboard.service,pipeline@.service,midday.timer,close.timer}
systemctl --user daemon-reload
```

## 数据源与自动兜底

项目用两个行情源，实测（2026-11-12）：

| 源 | 用途 | 稳定性 |
|---|---|---|
| 东财 `push2.eastmoney.com` clist | 全市场列表、实时价、**主力资金流**、行业 | 间歇性整链不可用（备用域名和明文 HTTP 同时失效） |
| 同花顺 `d.10jqka.com.cn` | 个股日K、全市场实时快照兜底 | 10/10 稳定 |

`live_quotes.py` 的处理顺序：

1. 东财 56 页并发拉取，单页重试 3 次，失败页再降并发补一轮；
2. 若结果少于 4000 只，自动切**同花顺 realhead 逐只兜底**（约 12 秒 / 5023 只，覆盖 100%），
   缓存放宽到 60 秒以免把上游打爆；
3. 行情为空或滞后超过 150 秒 → **明确回退快照**，绝不再标「● 实时」。

兜底源不同花顺资金流字段（口径与东财冲突，方向可相反，不可信），
这部分保留最近快照值，看板会显示警告：
「东财列表不可用，价格来自同花顺（分钟级），资金流仍为快照值」。
也就是说，**兜底期间价格/涨跌/成交额是实时的，资金流是上一次采集的**。

## 已知限制

- `/api/run` 无鉴权，只适合本机或隧道访问。
- 非交易日由 `auto_collect` 用上证指数日K 是否更新判断，自动跳过，无需维护节假日日历。
- `auto_output/` 只保留运行产出，不归档历史快照；需要留历史得另做备份。
