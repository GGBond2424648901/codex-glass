# Codex Glass

**把 Codex 用量，变成桌面上清晰、轻盈的一块玻璃。**

Windows 原生悬浮组件与完整看板。展示模型 Token、美元费用估算及账号额度快照；支持跨电脑 SQLite 历史导入与自动去重。

**本地统计 · 原生桌面 · 历史去重 · 可调玻璃外观**

[下载 Windows EXE](https://github.com/GGBond2424648901/codex-glass/releases/latest) · [使用指南](DESKTOP_GUIDE_zh-CN.md) · [源码架构](docs/ARCHITECTURE.md) · [数据与隐私](PRIVACY.md) · [反馈问题](https://github.com/GGBond2424648901/codex-glass/issues)

> **4.1.5 更新**：按真实快照保留 Pro 的五小时 / 周窗口，增加旧快照与“部分预估”提示；延续 4.1.4 的低内存统计实现。Windows 257 项测试通过。[更新记录](CHANGELOG.md) · [验收记录](docs/VERIFICATION.md)

![原生完整看板](docs/images/overview-today.png)

> 截图由实际 Qt 界面渲染，使用演示数据，不是你的账单，也不是把设计图片贴进程序。窗口外侧透明；流彩、曲线、数字和按钮独立渲染。

## 两种窗口，一套体验

- **桌面组件**：费用、Token、模型趋势、有效额度。默认置顶，支持托盘收起、迷你模式与快捷展开。
- **完整看板**：总览 / 模型 / 历史 / 额度 / 设置，五个页面共享今日 / 5 小时 / 累计三档用量范围。
- **液态玻璃风格**：缓慢流彩、渐变曲线填充、圆角高光、透明度预设与动画开关。
- **窗口缩放**：拖动边缘等比例调整，控件与字体同步缩放。
- **图表探索**：滚轮以鼠标位置为中心缩放时间范围，悬停查看实际采样点，双击复位。
- **原生交互**：玻璃菜单、多选筛选器、详情浮层。完整看板不调用外部浏览器。

![桌面悬浮组件](docs/images/widget-5h.png)

## 看什么数据

| 页面 | 信息与操作 |
| --- | --- |
| 总览 | 费用估算、Token、缓存命中、调用次数、趋势、模型占比 |
| 模型 | 按模型或匿名工作区查看输入 / 缓存 / 输出 / 推理，搜索、多选、结构与价格覆盖 |
| 历史 | 时间 / 模型 / 工作区筛选、数值排序、分页、导出本页 CSV、导入 SQLite |
| 额度 | 当前账号有效窗口、剩余百分比、重置时间、快照时间 |
| 设置 | 透明度、预设、流彩 / 图表动画、置顶、窗口缩放、本地数据 |

<details>
<summary>展开更多界面：模型明细、历史筛选、外观设置</summary>

![模型明细](docs/images/models-today.png)
![历史多选筛选](docs/images/history-filter-open.png)
![外观设置](docs/images/settings-today.png)

</details>

## 快速开始

### 运行 EXE

从 [Releases](https://github.com/GGBond2424648901/codex-glass/releases) 下载 Windows 包，解压运行 `CodexGlass.exe`。无需 Python 或 Conda。

> **从旧版升级**：先从系统托盘退出旧版，再运行新版。无需删除或重建 SQLite，也不用重新导入历史。避免同时运行多个版本；不要为了排查问题结束所有 Python / Codex 进程。

程序优先连接本机 `127.0.0.1:8081`，服务不存在时自动启动本地后台；无关服务占用端口时自动换用空闲回环端口。默认扫描当前用户的 `.codex/sessions`，设置了 `CODEX_HOME` 则读取该目录下的 `sessions`，并在同一 Codex 数据目录建立监控索引。安装盘符和无项目任务工作文件夹不决定日志位置。大量历史首次汇总可能需要几十秒，窗口仍可操作。没有记录时显示空状态，不生成假数据；异常时可通过菜单“复制数据诊断”取得排查信息（含本机路径，请勿直接公开）。

EXE 未做商业代码签名。请核对 Release 的 `SHA256SUMS.txt`，不要关闭系统安全防护。

### 使用现有 Conda / Python

需要 Python 3.10+，桌面使用 PyQt5，后端只依赖标准库。

```powershell
conda activate <你的已有环境名>
python -m pip install -r requirements-desktop.txt
python desktop_widget.py
```

连接自定义本地服务：

```powershell
python web_dashboard.py --no-browser --port 8082
python desktop_widget.py --url http://127.0.0.1:8082 --no-start-backend
```

保留兼容的 Web / 终端入口，详见 `python monitor.py --help`。

## 数据来源与统计口径

```text
本机 ~/.codex/sessions/**/*.jsonl
              ↓ 增量解析 Token 事实
本地 SQLite ← 另一台电脑的 SQLite（显式导入、自动去重）
              ↓ 当前价格估算 + 紧凑历史分页
       悬浮组件 / 原生完整看板
```

- 使用本工具的 `codex-monitor.sqlite3` 索引，并非任意 Codex 内部 SQLite 都能直接读取。原始 Token 事实来自 `sessions`，不依赖账单 API。
- 费用按当前配置的文本 Token 单价重新估算，默认 Standard，**不等于订阅实扣或实际账单**。未区分 Fast / Batch / Flex，未单独计入缓存写入、图片、音频及工具费用。未知模型显示“未计价”；小窗口当前范围包含未定价模型时显示“部分预估”。
- 缓存包含在输入中，推理包含在输出中，不能重复相加。
- 今日按本机自然日；5 小时回溯当前时间；累计覆盖全部已合并历史。曲线使用五分钟桶 Token/min 或每日 Token，空桶为 0。
- 用量范围不等于账号额度。切换时间范围不改变当前额度或全局设置。
- 当前额度只采用最新有效的**本机**官方 `rate_limits` 快照。按实际返回显示周 / 五小时窗口，不因 Pro 套餐名称隐藏已有窗口；没有或过期的窗口不补造、不假设恢复 100%。
- 额度不是实时联网查询。超过五分钟的记录标记“快照较旧”；其他电脑继续使用后，本机没有新记录就可能显示旧值。鼠标移到额度上查看快照及重置时间。
- 原始本机记录可补齐旧索引未保存的套餐或第二窗口。无法访问时只展示可验证的已保存信息。

## 跨电脑历史合并

1. 在旧电脑导出本工具的 SQLite 索引。复制前关闭后台，或使用 SQLite 一致性备份，避免漏掉 WAL。
2. 在完整看板的“历史”或“设置”选择 **导入 SQLite**，选择解压后的 `.sqlite3` / `.sqlite` / `.db`。
3. 确认后校验来源、备份目标、按稳定会话事实去重合并。大文件导入需要时间，可继续使用界面。
4. 重复导入不重复计数；导入历史与本机事实重叠时也会去重。

```powershell
python monitor.py import-index --source-index "D:\history\old-computer.sqlite3"
```

不要手工拼接 SQLite 表，也不要将 `sessions` 与索引视为两份账单相加。其它操作见 `python monitor.py --help`。

> 若提示 `Source SQLite database cannot be the target database`，说明选择了正在使用的目标索引。请改选另一台电脑导出的索引，而不是再次导入本机自身。

## 性能

完整历史事实保留在 SQLite；后台汇总使用紧凑列式数组与共享价格维度，历史明细按页展开。界面不会一次接收全部历史，但后台仍需处理完整数据，并非恒定内存或零成本查询。

**最近同样本对比：533,293 条用量事件，4.1.3 → 4.1.4 内存优化测量。** 每个版本在独立进程中连续重算三次，刷新时保留上一轮结果：

| 后台指标 | 4.1.3 | 4.1.4 优化测量 |
| --- | ---: | ---: |
| 三轮刷新最高 RSS | 439.0 MiB | 188.1 MiB（约下降 57%） |
| 汇总后 RSS（显式 GC 后） | 152.5–162.2 MiB | 76.4–80.0 MiB |
| 完整重算耗时 | 17.0–17.6 秒 | 22.4–26.9 秒 |

这是**内存优先**的取舍：完整重算耗时有所增加，完整统计和逐条历史摘要一致。

**4.1.5 最终 EXE 单独实测**：同规模隔离副本，21.07 秒观察到完整用量、25.40 秒全部就绪；后台观测峰值 149.6 MiB、就绪时 96.3 MiB，未强制 GC。它与上表的三轮源码测试流程不同，不能直接拼成同一组对比。

> 数字仅代表本机样本，不是整个 EXE 的固定占用承诺。Qt 界面、启动器、历史规模、导入来源和系统负载均有额外影响。采样也可能遗漏短暂峰值。测试方法、精确数据和早期 610,931 条样本记录见 [性能说明](docs/PERFORMANCE.md)。

无新事实且价格未变时复用历史，按分钟时钟更新滚动统计；手动刷新立即安排重算。界面每五秒检查，隐藏窗口停止无用动画及页面重建。检查频率不等于全历史重算耗时。

## 开发与构建

4.1 起实现代码归入 `codex_glass/`：核心计算、SQLite 存储、服务、桌面组件和命令行工具各有独立目录。根目录仅保留三个兼容启动入口；Web 页面从 Python 服务中提取为资源文件。详见 [源码结构与维护指南](docs/ARCHITECTURE.md)。此次整理不改变界面或数据库结构，无需重新导入历史。

```powershell
python -m pip install -r requirements-desktop.txt -r requirements-dev.txt
python -m black --check codex_glass tests monitor.py web_dashboard.py desktop_widget.py
$env:QT_QPA_PLATFORM = 'windows'
python -m unittest discover -s tests -v
python -m pip install -r requirements-build.txt
./build_exe.ps1
```

托盘 / 桌面合成测试需要交互式 Windows 桌面。`tests/capture_dashboard.py` 生成 15 个实际页面状态的演示截图；生产界面不加载测试数据。详见 [验收说明](docs/VERIFICATION.md)。

## 限制与隐私

- EXE 面向 Windows x64；其它平台桌面外观尚未完整验收。
- 玻璃为程序绘制的透明漫射材质，不是对桌面内容实施系统级高斯模糊；避免整窗 DWM 背板导致圆角外漏色。
- 动态光影、字体与 DPI 会造成差异，不宣称 AI 设计图与所有机器逐像素完全相同。
- 不提供云同步、实际账单获取或跨账号身份识别。
- 默认只监听本机，不上传记录。仓库 / Release 不含个人 SQLite、会话、日志、令牌或虚拟环境。不要把监控端口暴露到公网。
- 原始设计稿与本地设计对照图不上传；文档配图是实际程序使用演示数据生成的界面截图。

参见 [PRIVACY.md](PRIVACY.md) 与 [SECURITY.md](SECURITY.md)。

## 许可与致谢

基于 [SC123667/codex-monitor](https://github.com/SC123667/codex-monitor) 的本地监控核心继续开发，保留原作者和贡献者的 [MIT 许可](LICENSE)。本项目不是 OpenAI 官方产品。

桌面发行物包含 PyQt5 / Qt，适用各自许可；分发时须保留许可声明及对应源码获取方式。见 [第三方许可说明](THIRD_PARTY_NOTICES.md)。
