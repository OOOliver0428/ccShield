# 参与 ccShield

感谢你愿意帮助改进 ccShield。项目涉及真实直播间登录态和房管操作，任何贡献都应优先保证账号、Cookie 与直播间用户安全。

## 开始之前

- Bug 和功能建议优先通过对应的 Issue 表单提交。
- 涉及较大交互、接口或数据结构调整时，请先在 Issue 中说明目标和方案，避免重复工作。
- 不要在 Issue、PR、截图、日志或测试夹具中提交 `SESSDATA`、`bili_jct`、二维码、个人快捷房间配置或其他账号信息。
- 自动化测试不得连接真实 B站，也不得执行禁言、解禁等写操作。

## 本地开发

请先安装 [uv](https://docs.astral.sh/uv/) 和 Node.js 或 Bun，然后运行：

```bash
make dev
```

也可以使用根目录的 `start.cmd`、`start.sh` 或 `start.command`。更完整的环境和测试说明见：

- [配置说明](docs/config.md)
- [测试策略](docs/testing.md)
- [冒烟测试](docs/smoke_test.md)
- [安全模型](docs/security.md)

## 分支与提交

- 功能分支使用 `feat/<short-name>`，Bug 修复使用 `fix/<short-name>`，文档使用 `docs/<short-name>`。
- 提交信息建议使用 Conventional Commits，例如 `feat: ...`、`fix: ...`、`docs: ...`、`test: ...`、`ci: ...`。
- 一个 PR 聚焦一个目标；无关格式化或重构请拆分提交。
- 不要直接提交本机 `.env`、`config/quick_rooms.json`、日志、缓存或开发代理工作目录。

## 提交前检查

后端：

```bash
cd backend
uv sync --extra dev
uv run ruff check .
uv run basedpyright
uv run pytest
```

前端：

```bash
cd frontend
bun install --frozen-lockfile
bun run typecheck
bun run lint
bun run test
bun run build
```

如果修改了后端 API，还必须重新生成并提交 OpenAPI 描述与 TypeScript 客户端：

```bash
bash scripts/gen_schema.sh
bash scripts/gen_client.sh
```

## PR 要求

- 清楚说明问题、根因、改动和用户影响。
- 为行为变更补充自动化测试；无法自动测试时说明人工验证范围。
- 用户可见行为、配置或发布方式发生变化时同步更新 README、相关文档和 `CHANGELOG.md`。
- 房管写操作的真实验证必须事先说明房间、对象、期限和恢复方案，并获得仓库维护者明确同意。
