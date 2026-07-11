# ccShield

哔哩哔哩直播间弹幕管理工具，包含 FastAPI 后端和 Vue 前端。

## 一键启动

运行前只需要安装：

- [uv](https://docs.astral.sh/uv/)；
- Node.js（自带 npm）或 Bun。

启动器会自动同步后端依赖；如果前端依赖不存在，也会自动安装。服务就绪后会打开
<http://127.0.0.1:5173>。在启动终端按 `Ctrl+C` 会同时关闭前后端。

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
