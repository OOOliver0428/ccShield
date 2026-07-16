# ccShield 文档

本目录保存面向使用者、贡献者和维护者的长期文档。临时调试记录、开发代理状态和一次性验证证据不属于项目文档，不应提交到仓库。

## 使用与配置

- [配置说明](config.md)：本地 `.env`、快捷房间状态和网络绑定约束。
- [v2.1.0 发布方案](plans/v2.1.0.md)：首个正式 Release 的范围、决策与发布门槛。

## 开发与验证

- [测试策略](testing.md)：单元测试、合成响应、可选只读 live 测试及安全边界。
- [发布指南](release.md)：Windows 成品构建、验证、标签、发布和回滚流程。
- [冒烟测试](smoke_test.md)：安全成品检查与必须单独授权的真实操作验证流程。
- [API 客户端](api-client.md)：OpenAPI 描述和前端生成客户端的同步方法。

## 安全

- [安全模型](security.md)：本机运行、Cookie、LOCAL_TOKEN 和威胁边界。
- [漏洞报告策略](../SECURITY.md)：如何私下报告安全问题。

贡献流程见仓库根目录的 [CONTRIBUTING.md](../CONTRIBUTING.md)。
