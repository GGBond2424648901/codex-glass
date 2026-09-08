# Codex Monitor 便携与多电脑使用指南

> 4.0 更新：现在可在原生完整看板中直接导入另一台电脑的 SQLite，并自动去重；参见 [桌面指南](DESKTOP_GUIDE_zh-CN.md)。下文复制原始 sessions 的方式仍可用，但已不是唯一合并方式。不要直接拼接数据库表。

本项目可作为不携带个人数据的源码包在不同电脑上安装。每台电脑默认只读取自己保存的 Codex 会话日志；如需跨电脑历史汇总，请按本文的“多电脑汇总”流程建立一份新的汇总索引。

## 本地索引如何工作

Web 仪表板默认读取 `~/.codex/sessions`（Windows 为 `%USERPROFILE%\.codex\sessions`）。首次启动时，会解析该目录下已有的会话日志，并将 token 使用事实写入本地 SQLite 索引；后续启动和刷新只增量解析新增或追加的内容。SQLite 默认位于 `~/.codex/codex-monitor.sqlite3`（Windows 为 `%USERPROFILE%\.codex\codex-monitor.sqlite3`）。

SQLite 保存的是从会话日志解析得到的使用事实，而非固定的费用结论。因此，当内置定价策略、本地 `pricing_per_million` 覆盖或模型别名映射改变时，监控器会基于 SQLite 中已有的 token 事实重新聚合并计价，无需重新扫描所有未改变的原始会话文件。汇总缓存键包含定价策略版本和定价核对日期，也包含本地定价/别名配置等会影响公开汇总的输入，避免把旧价格的汇总当作新价格结果使用。

原始会话日志仍是最重要的源数据。SQLite 是本机派生索引，方便快速增量读取、汇总和重估；不要把它当作跨设备同步格式或唯一备份。

## 安装与启动

要求：Python 3.10 或更高版本。在源码包根目录执行以下命令。

### Windows（PowerShell）

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python web_dashboard.py --no-browser
```

如果系统未安装 Python Launcher（没有 `py` 命令），请使用已在 `PATH` 中的 Python：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python web_dashboard.py --no-browser
```

如果 PowerShell 的执行策略阻止激活，可在当前窗口临时允许脚本，或不激活环境而直接调用虚拟环境中的 Python：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1

# 或者
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe web_dashboard.py --no-browser
```

### macOS、Linux 和其他兼容平台

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python web_dashboard.py --no-browser
```

默认监听地址是 `http://127.0.0.1:8081`。`--no-browser` 只是不自动打开浏览器，服务仍会启动；随后在本机浏览器打开该地址即可。默认仅监听本机回环地址，不会对局域网公开。

若会话日志位于其他位置，请显式指定其根目录：

```bash
python web_dashboard.py --sessions-dir "/path/to/sessions" --no-browser
```

Windows 示例：

```powershell
python web_dashboard.py --sessions-dir 'D:\CodexSessions' --no-browser
```

`--sessions-dir` 应指向包含会话文件的根目录。若为不同根目录建立索引，请同时使用不同的 `--index-db`，以避免混入历史。

## 账号级官方额度快照与本地历史

仪表板中的官方额度快照和历史 token/费用不是同一种数据：

- **官方额度快照**来自本机 Codex 会话日志中的 `rate_limits` 字段，是最近一次在该电脑上观测到的账号级窗口信息。换一台电脑登录同一账号并产生 Codex 会话后，新日志中的快照也可以显示该账号的窗口状态。
- **本地历史**来自这台电脑当前保存的原始 sessions。默认的 token、事件数和费用估算只覆盖该电脑保留的会话，不会自动补齐另一台电脑、网页端或已删除日志中的使用。

监控器不会直接远程拉取完整账单、完整账户历史或其他电脑上的日志。官方快照适合查看最近观测到的额度窗口；本地历史适合分析已落在本机文件中的使用趋势。

## 多电脑汇总：推荐流程

需要把多台电脑的历史放到一个汇总机查看时，优先复制每台电脑的**原始 sessions** 到汇总根目录下彼此独立的子目录，再让本项目为该汇总根目录新建 SQLite 索引。例如：

```text
D:\CodexSessionArchive\
  laptop-a\
    sessions\
      ...
  desktop-b\
    sessions\
      ...
```

然后在汇总机上指定汇总根目录，并使用一份新的数据库：

```powershell
python web_dashboard.py --sessions-dir 'D:\CodexSessionArchive' --index-db 'D:\CodexSessionArchive\codex-monitor.sqlite3' --no-browser
```

操作要点：

- 不要直接合并 SQLite 数据库或其 `-wal`、`-shm` 辅助文件；它们是本机派生状态，不是可靠的跨库合并格式。
- 不要把同一个会话文件复制到多个子目录，否则它会被重复统计。为每台来源电脑保留固定的独立子目录。
- 源机的 SQLite 还可能保留“此前已索引、但原始 sessions 后来被删除”的历史。因此，只复制当前仍存在的 sessions 不一定能重现源机仪表板的完整历史；这也是汇总结果应注明来源和覆盖范围的原因。
- sessions 由 Codex 写入，监控器只读取 sessions 并写入 SQLite。复制 sessions 前应等待或停止源机 Codex 的写入，避免复制到正在追加的文件；若因本地备份而复制索引，则先停止监控器。跨电脑汇总仍不推荐搬运、合并或复用 SQLite，应该只复制稳定的原始 sessions，再在汇总机新建索引。

## 只有 SQLite 历史时：导入并去重

若另一台电脑只保留了 Codex Monitor 导出的标准 SQLite 历史索引，可以将其永久导入当前索引。导入前先停止仪表板，避免与正在运行的索引器争用写入租约：

```powershell
python monitor.py stop
python monitor.py import-index --source-index 'D:\History\other-computer.sqlite3'
python monitor.py background
```

使用非默认目标数据库时，同时提供 `--index-db`。导入命令会只读验证源库、在目标旁创建带时间戳的备份，并在一个事务中复制事实。重复执行相同命令不会重复计数；不同来源中重叠的同一会话事件也只汇总一次。本机原始 sessions 产生的事实优先于导入副本，改变定价后所有本机与导入事实会一起重新计价。

查看或删除导入来源：

```powershell
python monitor.py list-imports
python monitor.py remove-import --source-id '<来源 ID>'
```

普通 `--rebuild-index` 只重建本机 sessions，已导入历史会保留。删除来源只删除该来源关联的导入事实，不影响本机历史或其他来源。源 SQLite 中的汇总缓存、运行状态和会话根绑定不会导入。

汇总后的费用仍是按当前 API 单价对已知 token 使用量做出的估算，不是 Codex 订阅的实际扣费、发票或官方结算金额。

## 打包与安全检查

用于分发的源码包不应包含任何个人本地数据或运行产物，尤其不要包含：

- `sessions` 或任何 `*.jsonl` 会话日志
- SQLite 数据库及其 `-wal`、`-shm` 文件
- 日志和 PID 文件
- API 密钥、令牌、`.env` 或个人配置
- `.venv` 虚拟环境
- `__pycache__`、`*.pyc` 等 Python 缓存

建议只从干净的源码工作树打包，并在发送前检查压缩包的文件清单。即使 SQLite 只是派生索引，它也可能含有路径、模型、时间和 token 使用元数据，应与原始 sessions 一样视为私密数据。
