# 午盘/收盘自动运行

统一入口：

```bash
python run_stock_session.py --session midday --stocks normalized_snapshots.json
python run_stock_session.py --session close --stocks normalized_snapshots.json
```

默认使用：

```text
代理：http://127.0.0.1:7897
H5 离线回退：dynamic/h5_api
输出：scheduled_output/
数据库：scheduled.sqlite3
```

macOS LaunchAgent 模板：[com.ths.research.sessions.plist](./com.ths.research.sessions.plist)

安装前将 `ProgramArguments` 中的 `--session close` 改成所需模式，并执行：

```bash
mkdir -p ~/Library/LaunchAgents
cp com.ths.research.sessions.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ths.research.sessions.plist
```

午盘和收盘需要分别建立两个 Label/Plist，避免同一任务的时间配置互相覆盖。输入快照必须在运行前更新为当天数据。
