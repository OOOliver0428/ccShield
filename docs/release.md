# 发布指南

本文面向 ccShield 维护者。v2.1.0 首发 Windows x64 免安装包；Linux 与 macOS 继续使用源码启动脚本。

## 成品结构

GitHub Release 提供：

```text
ccShield-v2.1.0-windows-x64.zip
ccShield-v2.1.0-windows-x64.zip.sha256
```

压缩包内包含 `ccShield/ccShield.exe`、只读运行库、MIT License 和便携版说明。Cookie、快捷房间与日志不进入压缩包，运行时写入 `%LOCALAPPDATA%\ccShield`。

## 本地候选构建

在仓库根目录执行：

```powershell
cd frontend
npm run build
cd ..

$env:UV_CACHE_DIR = "$PWD\.uv-cache"
uv sync --project backend --extra dev --extra release --python 3.11
uv run --project backend --python 3.11 pyinstaller packaging/ccshield.spec --noconfirm --clean
```

将发行说明放入成品后执行安全冒烟：

```powershell
Copy-Item LICENSE dist\ccShield\LICENSE.txt
Copy-Item packaging\RELEASE-README.txt dist\ccShield\README.txt
.\dist\ccShield\ccShield.exe --smoke-test --no-browser --port 18765 --data-dir "$env:TEMP\ccshield-release-smoke"
```

冒烟测试仅访问本地接口，不连接真实直播间。完整安全边界见 [冒烟测试](smoke_test.md)。

## 发布前门槛

- `uv run ruff check .`、`uv run basedpyright` 和全部非 live 后端测试通过。
- 前端 typecheck、lint、全部测试和生产构建通过。
- `python scripts/check_version.py --tag v2.1.0` 通过。
- 打包后的健康接口、首页、登录引导和 WebSocket 握手通过。
- 深色、浅色、横屏与电脑版竖屏完成浏览器检查。
- `git diff --check` 和凭据扫描通过，仓库不包含 `.env` 或个人快捷房间。

## 正式发布

1. 将完整改动合入 `main`，确认 `ci-main` 全部通过。
2. 创建并推送带注释标签 `v2.1.0`。
3. `.github/workflows/release.yml` 在干净的 Windows runner 上重新构建前后端。
4. 工作流运行成品冒烟，生成 ZIP 与 SHA-256，并创建 GitHub Release。
5. 下载 Release 成品，在一台普通 Windows x64 机器上解压并双击复核。

不要手工上传未经工作流验证的本地压缩包覆盖自动产物。

## 失败与回滚

- 构建或冒烟失败：不创建 Release，修复后提交新改动，再重新创建标签。
- Release 已创建但成品有严重问题：先将 Release 标记为预发布或撤下资产，保留问题记录；修复后发布新的补丁版本，不复用已公开标签。
- 禁止强制移动一个已经公开的版本标签，否则用户无法可靠校验源码和成品。

Windows 成品当前未代码签名。发行说明和压缩包内 README 必须保留 SmartScreen 提示、仓库下载地址和 SHA-256 校验说明。
