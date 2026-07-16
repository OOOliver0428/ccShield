## 变更说明

<!-- 说明问题、根因、改动内容和用户影响。 -->

## 验证

<!-- 列出已运行的自动化测试和必要的人工验证。 -->

## 安全检查

- [ ] 未包含 `.env`、`config/quick_rooms.json`、Cookie、二维码、Token、日志或开发代理工作目录。
- [ ] 自动化测试未连接真实 B站，未执行禁言、解禁等房管写操作。
- [ ] 新增或修改的测试使用合成响应、MockTransport 或前端 HTTP Mock。
- [ ] 如修改后端 API，已同步更新 OpenAPI 描述和生成的 TypeScript 客户端。
- [ ] 如改变用户可见行为，已更新相关文档和 `CHANGELOG.md`。

## 关联 Issue

<!-- 例如：Closes #123 -->
