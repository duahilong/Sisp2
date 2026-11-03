# Sisp2

Sisp2 是面向 Windows 的硬盘装机工具，可规划并执行清盘、GPT EFI/Windows/Data 分区、应用 WIM、写入 UEFI 引导及分区结果校验。真实执行会破坏目标盘全部数据；在隔离环境外使用前必须逐项核对目标盘。

## 环境与安装

要求 Windows 与 Python 3.10+。CLI 主体使用标准库和系统工具；Web GUI 另需 FastAPI、uvicorn、pywebview（仅桌面窗口需要 pywebview）。

```powershell
python -m pip install -r requirements.txt
# 可选：GUI 服务及桌面窗口
python -m pip install -r requirements-web.txt
```

## 启动

```powershell
# CLI：列出硬盘（只读）
python -m app.cli --list
# CLI：演练，不提交任务
python -m app.cli --dry-run
# CLI：真实执行示例；须明确限定盘号并使用管理员权限
python -m app.cli --allow-disks 1
# CLI：真实执行并把命令日志写入指定目录（可选）
python -m app.cli --allow-disks 1 --log-dir D:\logs

# GUI 桌面窗口
python -m app.gui_web.main
# GUI 浏览器模式
python -m app.gui_web.main --serve
# 显式演练启动
python -m app.gui_web.main --dry-run
```

CLI 默认 `--dry-run` 为关闭状态；省略 `--dry-run` 的 CLI 可进入真实执行路径，但仍要求白名单、管理员权限、身份复核等条件。GUI 的实际模式由命令行 `--dry-run` 与设置 `dryRunByDefault` 共同决定：任一为真即演练，且该演练状态是硬上限，启动后的界面操作或 Web API 不能把它升级为真实执行。二者都为假时可能进入真实模式；启动 GUI 不等同于安全演练，请显式传 `--dry-run`。

GUI 默认监听 `127.0.0.1:8765`；可用 `--host`、`--port` 改变监听参数。接口文档默认在 `/docs`。非 loopback 监听没有鉴权，属于高风险配置，详见[安全边界](Docs/security.md)及[Web API](Docs/web-api.md)。

## 打包成 exe

不想装 Python 的话，可以直接打成单文件 exe（免安装、双击即用）：

```powershell
python -m pip install -r requirements-build.txt
python packaging/build.py
# → dist/Sisp2.exe（图形界面，约 18 MB）、dist/Sisp2-CLI.exe（命令行，约 9 MB）
```

exe 的运行参数与源码运行完全一致，`--list` / `--dry-run` / `--allow-disks` / `--serve` / `--settings` 都可用；首次运行会在 exe 同目录生成可编辑的 `json/win11.json`，设置仍保存在 `%APPDATA%\Sisp`。细节（提权、日志位置、体积、未签名等限制）见[打包说明](Docs/packaging.md)。

## 配置

业务 JSON 默认路径为 `json/win11.json`；可通过 CLI/GUI 的 `-j` 或 `--config` 指定，或使用 `SISP_CONFIG`。文件描述 `partition_info`、`image_info` 和 `excluded_disk_names`。示例中的镜像路径是占位值，应按实际环境调整。

界面设置与业务配置分离，默认保存于 `%APPDATA%\Sisp\settings.json`（无 `APPDATA` 时为用户目录下 `.config/Sisp/settings.json`）；GUI 可用 `--settings` 指定位置。表内字段均已接入运行时，每个字段的真实行为、生效时机以及仍需说明的边界（例如 `language` 只影响页面 `lang`、外部工具类路径只参与预检）列于[配置说明](Docs/configuration.md)。

## 当前安全行为与限制

- `SispService` 默认 dry-run；CLI 和 GUI 入口策略不同，参见上方说明。
- CLI 真实执行必须有 `--allow-disks`。GUI 若没有显式白名单，真实模式下当前工作流会将本次选择的硬盘加入服务白名单；这不应被误读为 CLI 同等的显式授权约束。
- 写盘要过五道闸门：非 dry-run、白名单、强身份（UniqueId 或 SerialNumber）、执行前重扫比对、执行瞬间仍可选；受保护硬盘不能操作。
- 界面默认开启 `typeToConfirm`（真实执行须手输盘号或型号）与 `confirmBeforeStart`（须勾选已核对目标磁盘）；两者可在设置里关闭，关闭后只剩白名单与身份校验。
- Web 默认 loopback。Host 校验仅校验 Host 名称、不是身份认证；改为非 loopback 监听时没有鉴权，网络可达的写盘接口具有高风险。
- SSE 重放依赖进程内有限缓冲；队列满会丢弃新事件，缓冲淘汰也会使旧事件无法补发，不能保证完整事件历史。
- 预检仅在配置提供镜像路径时检查镜像文件是否存在；未提供路径不保证在预检阶段报出，真实执行器仍会拒绝缺少镜像路径。

详见[安全边界](Docs/security.md)。

## 测试

```powershell
python -m pip install -r requirements-web.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

测试主要依赖替身与注入实现，目标是验证逻辑而非真实设备写盘；不能据此声称 Windows 实际写盘流程已端到端验证。Web API 测试需要 `httpx`；SSE 测试会在后台线程启动真实 uvicorn（随机空闲端口），因此需要 `uvicorn`。不要在真实磁盘环境中尝试写盘测试。

## 设计原型

[`design/`](design/) 目录包含 `disk-tools-app.html` 与 `disk-tools-prototype.html` 两个可点击 HTML 界面原型，用于设计参考；原型不连接本项目后端，也不执行磁盘操作。正式运行界面位于 `app/gui_web/ui/index.html`。

## 文档索引

- [架构与工作流](Docs/architecture.md)
- [安全边界与已知风险](Docs/security.md)
- [业务配置与界面设置](Docs/configuration.md)
- [Web API、SSE 与部署](Docs/web-api.md)
- [测试与真实写盘验证](Docs/testing.md)
- [打包成 exe](Docs/packaging.md)
- [前端开发说明](app/gui_web/ui/README.md)
