# Security

## 凭据

不要把 API Key 写进命令、Prompt、配置文件、日志、测试 fixture 或 Issue。APIMart 使用 `APIMART_API_KEY`；自定义端点默认使用 `CUSTOM_IMAGE_API_KEY`，也可以配置其他环境变量名。

仓库不包含 `.env` 文件或生产凭据。示例测试值只用于验证脱敏，不对应任何真实服务。

## 报告安全问题

请提交不含真实凭据、个人数据或私有内容的最小复现。若复现本身含敏感信息，请不要创建公开 Issue；改用仓库所有者提供的私密联系方式或 GitHub 私密漏洞报告功能。

## 重要边界

- 生成 POST 不自动重试；结果不明时先核查 Provider 任务或账单。
- 不自动切换 Provider 或重新生成。
- 网络端点仍可能受到 DNS rebinding 等运行环境风险影响；请仅配置可信 Provider。
- 自定义兼容端点的能力必须由使用者明确声明，Skill 不推测质量或参考图能力。
