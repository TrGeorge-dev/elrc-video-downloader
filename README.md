# ELRC 录像下载器

上海科技大学 ELRC 单节课程回放下载工具。粘贴回放链接，在 Chrome 中登录，自行选择画面和保存位置，将原始 MP4 保存到本机。

适用于 Windows 10/11 和 macOS，需要 Google Chrome。从源码运行需要 Python 3.10+（含 tkinter）。这是独立项目，与学校及 ELRC 平台无隶属关系。

> **使用前请阅读 [使用声明与免责声明](DISCLAIMER.md)**：本项目为非官方工具；请确认你有权访问、保存和使用相关课程内容。能够播放不等于获得下载或传播授权，MIT 许可证仅适用于本项目代码。

## 使用

安装 [Google Chrome](https://www.google.com/chrome/) 后，按系统启动下载器。

- **macOS**：解压应用包，双击 **ELRC 录像下载器.app**。应用包自带 Python 和依赖。Apple 芯片版适用于 arm64 Mac。从源码启动或打包的方法见下文。
- **Windows**：解压源码，安装 [Python](https://www.python.org/downloads/windows/) 并保留 Tcl/Tk 支持，启用 Python Launcher 或加入 PATH。双击 **启动.cmd**。首次启动需要联网安装依赖。

1. 从 ELRC 复制具体课次的回放链接，粘贴后点击 **解析链接**。链接格式为 `https://elrc.shanghaitech.edu.cn/learn/videoreview/课程标识?...`，请完整保留周次、星期、节次等参数。
2. 在工具新打开的 Chrome 窗口中完成学校统一身份认证，登录成功后自动继续。
3. 勾选需要的教师近景、教师全景、屏幕等画面，可以多选，默认不勾选。
4. 选择保存文件夹，点击 **下载所选**。下次会记住这个文件夹。

录像保留原始 MP4，不转码；文件名包含课程、日期或周次、画面名称与部分资源编号。下载期间请保留工具打开的 Chrome 窗口。

## 支持范围

- 单节回放页面实际加载、当前账号可以观看的 ELRC MP4。
- 多画面选择、下载进度、停止、在服务器支持时断点续传。
- 下载结束检查文件长度及 MP4 容器结构，避免把登录页或不完整响应当成录像。
- 已完成且记录匹配的文件会跳过；不覆盖已有的其他同名文件。

当前版本不提供课程搜索或整门课遍历，不处理直播、仅提供 HLS 的页面、DRM 媒体或其他学校域名。账号无观看权限时会报错，不绕过访问控制。请仅下载获准保存的课程内容，分享录像前取得相应许可。代码的 MIT 许可证不授予课程视频的使用或传播权利。

## 登录状态与本地文件

工具使用专用 Chrome 配置目录，不读取日常 Chrome 的账户。密码在学校登录页面输入，应用代码不采集密码；浏览器会在本地保存 Cookie 等登录状态。

macOS 的状态目录是 `~/Library/Application Support/ELRCVideoDownloader`，Windows 使用 `%LOCALAPPDATA%\ELRCVideoDownloader`。其中 `chrome-profile` 保存登录状态，`settings.json` 记住保存文件夹。Windows 启动器将依赖安装到 `runtime`。**不要上传或分享状态目录**，也不要在 issue 中粘贴 Cookie、请求头或含令牌的地址。工具不更改代理、VPN 或系统网络设置。

下载文件旁可能出现以下记录：

| 文件 | 用途 |
| --- | --- |
| `录像.mp4.part` | 未完成的录像数据 |
| `录像.mp4.part.json` | 续传信息，包括源地址的 SHA-256 摘要、长度和服务器版本标记 |
| `录像.mp4.elrc.json` | 完成记录，用于识别已有下载 |

记录不保存 Cookie、密码或完整源地址。完成校验后才将临时文件改名为 `.mp4`。容器结构校验不等于逐帧解码校验。

## 常见问题

**停止后如何继续？** 再次解析同一课次，选择相同画面和目录。只有源地址和视频版本可核实时才续传；服务器忽略 Range 时从头下载。源地址或视频版本变化、临时文件缺少记录时，按提示选择新文件夹重新下载。

**已有同名文件？** 只有完成记录、源地址摘要、长度和容器结构匹配时才跳过，否则报错并保留原文件。需要另存时请选择其他目录。

**登录过期或无权访问？** 重新解析并在 Chrome 中登录；若仍无权限，请联系授课老师或平台管理员。

**找不到画面？** 确认粘贴的是具体课次回放链接、该节已发布且网页可播放 MP4。网站改版、未录制、未发布或不支持的媒体类型均可能导致解析失败。

**Chrome 启动失败？** 安装 Google Chrome，关闭上次由本工具打开的 Chrome 后重试。不要同时运行多个共用配置目录的实例。

**Windows 启动器找不到 Python？** 安装 Python 3.10+ 并保留 Tcl/Tk。启动器依次尝试 `ELRC_PYTHON` 指定的 `python.exe`、`py -3` 和 PATH 上的 Python。依赖来自 PyPI，启动器会优先使用已安装的 uv。

## 从源码运行

在仓库目录中执行对应系统的命令。

### macOS

安装带 Tcl/Tk 的 [Python](https://www.python.org/downloads/macos/)。下面的命令也用于准备打包环境。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python elrc_downloader.py
```

打包时安装 PyInstaller，再运行 `sh build-macos.sh`。应用会生成在 `dist` 目录。Apple 芯片版使用原生 arm64 Python 打包。

```sh
.venv/bin/python -m pip install pyinstaller==6.22.3
sh build-macos.sh
```

### Windows

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe elrc_downloader.py
```

在任一系统上，可以通过 `--state-dir` 指定专用登录及设置目录。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
```

测试使用合成数据、本机 HTTP 服务和模拟下载任务，不访问真实课程或读取登录配置。DOM 测试使用本机 Chrome，缺少 Chrome 时跳过；Tk 测试使用临时状态目录和模拟后台任务。GitHub Actions 在 Windows 上运行这些回归测试。

macOS 在同一环境运行 `.venv/bin/python -m pip install -r requirements-dev.txt` 和 `.venv/bin/python -m pytest tests -q`。

原版于 2026-10-08 完成真实 ELRC 单节回放测试：Chrome 登录、三路画面解析、复用登录状态，以及一份约 58 MB 录像的完整下载和结构校验。开源仓库仅包含代码及合成测试数据。学校网站更新后可能需要适配，自动测试不能代替实时平台验证。

核心文件：`elrc_downloader.py` 负责界面与浏览器，`transfer.py` 负责下载及校验，`launcher.ps1` 负责 Windows 环境准备。

实现参考：[Playwright 持久浏览器上下文](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context)、[Requests 流式下载](https://requests.readthedocs.io/en/latest/user/advanced/#body-content-workflow)。

## 许可证

代码采用 [MIT License](LICENSE)。[使用声明与免责声明](DISCLAIMER.md) 说明内容权限、隐私和责任范围，不修改 MIT 对软件代码授予的许可。
