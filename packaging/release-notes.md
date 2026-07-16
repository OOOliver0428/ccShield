## ccShield v2.1.0

这是 ccShield 首个面向普通用户的免开发环境版本。

### 主要变化

- 提供 Windows x64 免安装压缩包，无需安装 Python、uv、Node.js 或 Bun。
- 双击 `ccShield.exe` 后自动启动本地服务并打开浏览器。
- Cookie、快捷房间和日志统一保存在 `%LOCALAPPDATA%\ccShield`。
- 实时弹幕支持 12、14、16、18px 四档字号，并保持历史审阅位置。
- 展示当前用户在直播间中的主播、房管、观众或暂未识别身份。
- Cookie 运行中失效时安全停止旧连接并引导重新扫码。
- 延续 SC 单行横向审阅、1000 条弹幕缓存和基础禁言闭环。

### 下载与校验

下载 `ccShield-v2.1.0-windows-x64.zip`，解压后打开 `ccShield` 文件夹并双击 `ccShield.exe`。同目录提供 `.sha256` 文件用于校验下载完整性。

本版本尚未进行 Windows 代码签名，首次运行可能出现 SmartScreen 提示。请只从本仓库 Releases 页面下载。
