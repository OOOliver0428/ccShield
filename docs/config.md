# 配置与本地数据

ccShield 的源码运行版和 Windows Release 使用同一组配置键，但数据目录不同。两种模式都只在本机保存 Cookie 和快捷房间，不应将这些文件提交到 Git。

## Windows Release

免安装版将用户数据保存在：

```text
%LOCALAPPDATA%\ccShield
├── .env
├── config\quick_rooms.json
└── logs\ccshield.log
```

程序目录只包含只读运行文件。首次启动会在用户数据目录创建必要文件，并通过页面引导扫码登录。Release 不会自动读取或迁移源码仓库中的 `.env` 与快捷房间，避免意外复制个人凭据。

## 源码运行版

源码模式默认使用仓库根目录作为数据目录：

```text
<repo>/.env
<repo>/config/quick_rooms.json
```

缺少 `.env` 时会使用安全默认值并进入扫码登录流程。`.env`、`config/quick_rooms.json` 均已被 `.gitignore` 和 CI 安全检查排除，仓库只提交 `.env.example` 与 `config/quick_rooms.example.json`。

## 配置键

| 键 | 必需 | 默认值 | 用途 |
| --- | --- | --- | --- |
| `SESSDATA` | 登录后需要 | `""` | B站会话 Cookie，由扫码登录写入。 |
| `BILI_JCT` | 登录后需要 | `""` | B站 CSRF Cookie，与 SESSDATA 配套。 |
| `BUVID3` | 否 | 空 | 部分接口使用的可选设备 Cookie。 |
| `ROOM_ID` | 否 | 空 | 启动时的默认目标房间号。 |
| `HOST` | 否 | `127.0.0.1` | FastAPI 监听地址。 |
| `PORT` | 否 | `8000` | FastAPI 起始端口。 |
| `DEBUG` | 否 | `false` | 后端调试模式。 |

字段名区分大小写，其他键会被忽略。源码模式编辑 `.env` 后需要重启应用；Release 通常由扫码登录自动维护 Cookie，无需手动编辑。

## 路径覆盖（开发与测试）

`CCSHIELD_DATA_DIR` 可覆盖源码模式的数据目录，`CCSHIELD_STATIC_DIR` 可指定 FastAPI 提供的前端构建目录。它们主要用于打包和自动化测试，不建议普通用户手动设置。

Release 启动器会设置 `CCSHIELD_RELEASE=1`，并将数据目录固定到当前用户的本地应用数据目录。该模式不会执行旧版仓库 `.env` 迁移。

## 快捷房间

快捷房间属于个人状态。源码模式保存到 `<repo>/config/quick_rooms.json`，Release 保存到 `%LOCALAPPDATA%\ccShield\config\quick_rooms.json`。关闭 ccShield 后可以手动编辑该文件删除条目。

## 网络安全

默认 `HOST=127.0.0.1` 是安全边界的一部分：管理页面、登录令牌和房管 API 只对当前计算机开放。不要轻易改为 `0.0.0.0`；这会把管理界面暴露给局域网，并需要同步重新设计 CORS 和本地令牌校验。

`LOCAL_TOKEN` 在每次进程启动时随机生成并只在内存中保存。重启应用后，旧页面需要重新获取令牌。
