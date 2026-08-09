# Provider 与 CLI

只在用户完成本轮确认后调用生成渠道。API Key 只从环境变量读取；不得出现在参数、聊天、Prompt、元数据、错误或日志中。

## Codex 内置 imagegen

Codex 路径由 Agent 直接调用内置 `imagegen`，不得从 Python 脚本调用。新图传最终 Prompt；IP 参考或编辑任务按工具当前能力附带所需参考图片。该渠道不暴露独立质量 flag：不要伪造 `quality`，而是在 Prompt/工具调用中表达高质量意图并检查结果。

## APIMart

- 模型：`gpt-image-2`；密钥环境变量：`APIMART_API_KEY`。
- 创建：`POST https://api.apimart.ai/v1/images/generations`，字段为 `model`、`prompt`、`n`、`size`、`resolution`；有参考图时加 `image_urls`（最多 16 个安全 HTTPS URL 或 PNG/JPEG/WebP data URI）。
- 状态：`GET https://api.apimart.ai/v1/tasks/{task_id}`；等待态为 `pending`、`submitted`、`processing`，成功为 `completed`，终态失败为 `failed`、`cancelled`。
- 结果：按响应顺序展开 `data.result.images[].url[]` 中的有效 HTTPS URL，并立即下载时效结果。
- 质量：`draft→1k`、`standard→2k`、`high→4k`。
- GET 状态查询可在预算内退避重试；生成 POST 只提交一次。POST 超时或断线属于结果不明，不得重试、切换 Provider 或重新生成。
- 本地参考图只通过显式 `--reference-image-file` 读取：单文件最多 25 MiB，在当前进程内按 PNG/JPEG/WebP magic bytes 验证并转为 data URI；它与 `--reference-image` 互斥。`--brand loki` 不会自动或隐式注入参考图。需要默认 Loki 角色一致性时，显式传入 `<skill-root>/assets/brands/loki/character.png`。

## 自定义 OpenAI Images 兼容端点

要求用户提供 `base_url` 与 `model`，密钥环境变量名可配置，默认 `CUSTOM_IMAGE_API_KEY`。若 base URL 未以 `/images/generations` 结尾，脚本会追加该路径；同步响应接受 `data[].url` 或 `data[].b64_json`，每项必须且只能有一种结果。

默认仅允许 HTTPS。只有用户明确批准并设置 `--allow-local-http` 时，才允许 `localhost`、`127.0.0.1` 或 `::1` 的 HTTP；远程 HTTP 永远拒绝。

自定义端点的质量能力不能推定。未提供 `--custom-quality-map`、即端点未声明质量能力时，保留用户确认的抽象 `draft` / `standard` / `high` 意图，但省略、不发送 `quality` 参数。只有端点文档或用户配置已明确声明时，才传例如 `--custom-quality-map draft=low,standard=medium,high=high`；所选抽象档位必须存在于显式 map 中。确认卡和 `dry-run` 必须分别显示“省略”或实际声明映射，不得把示例当成端点能力。自定义渠道首版不支持参考图；不要展示或传入该能力。

## CLI 示例

先把 `<skill-root>` 解析为当前 `SKILL.md` 所在目录的绝对路径；以下命令可从任意工作目录执行。

先查看静态能力；此命令不读取 Provider 密钥：

```bash
python "<skill-root>/scripts/loki_image2.py" providers
python "<skill-root>/scripts/loki_image2.py" help
```

确认前只校验请求与映射，不联网、不生成、不创建输出：

```bash
python "<skill-root>/scripts/loki_image2.py" dry-run --provider apimart --prompt-file ./prompt.md --ratio 16:9 --quality standard --count 1
```

七项确认完成后才允许带 `--confirmed` 提交：

```bash
python "<skill-root>/scripts/loki_image2.py" generate --confirmed --provider apimart --prompt-file ./prompt.md --ratio 16:9 --quality standard --count 1 --brand loki --reference-image-file "<skill-root>/assets/brands/loki/character.png" --style "Loki Whiteboard Pro" --topic "agent-workflow"
```

`--brand loki` 只选择并记录品牌，不会自动把内置角色图注入 APIMart。需要角色一致性时，必须显式传入安全的 `--reference-image` HTTPS URL/data URI，或使用 `--reference-image-file` 传包内 Loki 图片；不要在文档、聊天或命令示例中嵌入真实 Base64 内容。

自定义端点还必须提供明确配置；密钥仍只在对应环境变量中：

```bash
python "<skill-root>/scripts/loki_image2.py" dry-run --provider openai-compatible --prompt-file ./prompt.md --ratio 1:1 --quality high --count 1 --base-url https://images.example/v1 --model custom-image-model --api-key-env CUSTOM_IMAGE_API_KEY --custom-quality-map draft=low,standard=medium,high=high
```

## 失败、日志与 live 门禁

- APIMart 可重试安全 GET，但不重试 POST；自定义同步生成同样只提交一次。
- APIMart 或自定义端点的 POST 发生模糊传输失败时，CLI 返回 `code=ambiguous_submission`、`billing_unknown=true`、`retryable=false`，明确要求先核查 Provider 任务/账单并且不重投；不得自动 fallback。
- Provider JSON 响应按 2 MiB 上限有界读取。图片按 25 MiB 流式上限读取，校验 Content-Type 与 PNG/JPEG/WebP magic bytes；合法 HTTPS 签名查询可下载，回显和日志会覆盖 `token`/`key`/`signature`、`X-Amz-*`、`X-Goog-*`、Azure SAS 与 CloudFront 参数。
- 下载会校验请求 URL 和 `response.geturl()` 返回的最终 URL，包括 scheme、凭据、host 与字面 IP 策略；非公开字面 IP 拒绝。标准库 `urllib` 没有本实现可用的逐跳回调，因此边界是“请求 URL + 最终 URL”，不声称逐个验证中间跳转。
- 失败后报告安全摘要和关联 ID，不自动 fallback、不自动重生成；让用户检查状态并重新确认后再决定。
- 脱敏错误日志位于 `~/.codex/loki-image/logs/`。参数解析成功后，若安全的 `logs` 日志目录已存在，则清理超过 7 日的 `.log` 文件；写错误日志时也先清理超过 7 日的文件。清理异常不阻断 `providers`/`dry-run`，不安全目录不遍历、不删除、不写入。目录不存在时成功命令不创建它；没有后台定时清理。不写完整 Prompt、原始输入、图片、密钥或 Authorization。
- CLI 的 stdout/stderr 始终各输出一行 UTF-8 JSON；安全 `help` 子命令不回显 argv。
- 真实冒烟测试不得自动运行；先明确确认 Provider、比例、质量、图片数量和可能费用，再执行一次最小 live smoke test。
