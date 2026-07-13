# ccShield

哔哩哔哩直播间弹幕管理工具，包含 FastAPI 后端和 Vue 前端。

当前版本：**v2.0.2**

![ccShield 金毛骑士产品图标](assets/brand/ccshield-product-mark-preview.png)

## 一键启动

运行前只需要安装：

- [uv](https://docs.astral.sh/uv/)；
- Node.js（自带 npm）或 Bun。

启动器会自动同步后端依赖；如果前端依赖不存在，也会自动安装。服务就绪后会打开
<http://127.0.0.1:5173>。如果 5173 已被占用，启动器会自动尝试 5174、5175 等后续端口，
并打开实际选中的地址。在启动终端按 `Ctrl+C` 会同时关闭前后端。

### Windows

双击 `start.cmd`，或者在 PowerShell 中运行：

```powershell
.\start.cmd
```

### Linux

```bash
chmod +x start.sh
./start.sh
```

### macOS

可以双击 `start.command`，也可以在终端中运行：

```bash
chmod +x start.sh start.command
./start.command
```

不希望自动打开浏览器时，追加 `--no-browser`。只检查本机依赖环境时，追加 `--check`。

项目配置只读取仓库根目录的 `.env`。首次使用时不创建该文件也能启动，并可在页面中扫码登录；
修改 `.env` 后需要重启服务。

## 快捷房间配置

登录后可在未连接状态点击“配置快捷房间”，使用短号或正常房间号验证主播、真实房间号和直播标题，
验证成功后保存为一键连接入口。连接直播间后，也可以点击“一键添加当前房间”。

快捷房间保存在本机的 `config/quick_rooms.json`，首次添加时自动创建，不会上传到仓库。
初版不提供页面删除功能；需要删除时，请先关闭 ccShield，再从该 JSON 文件的 `rooms` 数组中移除对应记录。
文件格式可参考 `config/quick_rooms.example.json`。

## v2.0.2 更新摘要

- 新增快捷房间：支持短号/正常号验证、快捷连接和一键添加当前房间；
- 快捷房间保存在本机 JSON 配置中，初版仅支持手动删除；
- 更换金毛骑士产品图标，并同步更新页面品牌标识和浏览器图标；
- Vite 默认端口被占用时自动向后选择可用端口；
- 优化电脑版竖屏布局，并修正实时同步状态文案。

完整版本记录见 [CHANGELOG.md](CHANGELOG.md)。
