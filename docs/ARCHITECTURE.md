# 源码结构与维护指南

4.1 将桌面、存储、服务和命令行实现分层。此次整理不修改费用公式、数据库结构或已有界面设计，4.0 的数据库无需迁移或重复导入。

## 目录职责

```text
monitor.py                 命令行启动入口
web_dashboard.py           兼容的本地服务启动入口
desktop_widget.py          桌面 / EXE 启动入口
codex_glass/
  core/                    用量解析、价格、展示数据投影（标准库）
  storage/                 SQLite 增量索引、历史导入去重、紧凑历史
  services/                本机 HTTP 服务与接口
  desktop/
    widget.py              悬浮组件与托盘
    dashboard.py           完整看板控制器、异步数据请求
    pages.py               五个页面，复用三个用量范围
    controls.py            表格、按钮、分段选择等共享控件
    data.py                桌面数值投影与 CSV 导出
    components/            曲线、玻璃材质、浮层、缩放宿主与样式
  cli/                     命令行管理与终端展示
  resources.py             源码 / 冻结 EXE 的资源路径
assets/
  web/dashboard.html       兼容 Web 看板的页面资源
  codex-glass.png           应用图标
  codex-glass.ico           Windows 图标
tests/                     临时数据库、演示数据与回归测试
docs/                      使用、性能、验收和演示截图
```

只有三个根目录 Python 文件是兼容启动入口，业务逻辑不在这些入口中。内部代码使用 `codex_glass.*` 的完整模块路径，避免重复导入、相对工作目录和同名模块造成的问题。

## 依赖方向

核心计算与存储层不依赖 Qt。服务层读取核心 / 存储，桌面层读取本地服务的数据；当前账号额度通过本机专用快照接口读取，不用导入历史中的额度替代。桌面显式导入操作调用存储层已有的校验、备份和去重事务。

包的初始化不启动网络、索引或窗口。`resources.py` 统一定位图标、HTML 与版本等资源，不以调用者当前目录推断位置。冻结包和源码运行都需要验证这些边界。

## 修改位置

| 需求 | 首先查看 |
| --- | --- |
| Token 解析与模型估算 | `core/usage.py` |
| 增量扫描、缓存与后台汇总 | `storage/index.py` |
| 导入、去重、备份与来源 | `storage/history_import.py` |
| 历史内存和分页 | `storage/compact.py` |
| 本机 API | `services/dashboard.py` |
| 小窗口、托盘 | `desktop/widget.py` |
| 完整看板页面 | `desktop/pages.py` 与 `desktop/dashboard.py` |
| 可复用玻璃 / 图表组件 | `desktop/components/` |

以上路径均相对于 `codex_glass/`。

## 开发约定

- 保持函数只承担明确职责，避免把控件绘制、数据库写入和后台请求混在同一方法里。
- 共享界面优先复用现有组件，不复制三套今日 / 5 小时 / 累计页面。
- Black 统一格式，避免多条逻辑压成一行；`pyproject.toml` 保存格式设置。
- 新功能和修复先补可复现的行为测试。原始 Token、费用公式、去重和额度来源的测试不能因搬文件而削弱。
- 对外兼容三个入口与现有 HTTP 参数；内部导入路径在 4.1 中已调整，不作为稳定第三方 API 承诺。
- 测试使用临时文件夹，不拿用户当前 SQLite 做删除、重建或导入实验。

## 验证与发布

```powershell
python -m pip install -r requirements-desktop.txt -r requirements-dev.txt
python -m black --check codex_glass tests monitor.py web_dashboard.py desktop_widget.py
$env:QT_QPA_PLATFORM = 'windows'
python -m unittest discover -s tests -v
python -m tests.capture_release work/captures
python -m pip install -r requirements-build.txt
./build_exe.ps1
```

发布包需要包含 HTML、图标与 VERSION，且须从无本机数据的 Git 提交生成源码归档。仅发布实际程序的演示截图；原始设计稿和本地设计对照图不属于公开源码包。
