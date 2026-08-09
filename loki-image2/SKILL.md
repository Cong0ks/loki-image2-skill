---
name: loki-image2
description: Use when a user wants to turn text, documents, audio, or images into a branded infographic plan, reusable image prompt, or generated image using Loki, a custom IP character, Codex imagegen, APIMart, or an OpenAI Images-compatible endpoint.
---

# Loki Image2

先把内容归一化一次，再严格停在 `plan`、`prompt` 或 `generate` 的边界。

| 模式 | 交付与停止边界 |
|---|---|
| `plan` | 只给分析、层级、布局、风格与比例建议；不写最终 Prompt，不生图。 |
| `prompt` | 给方案和最终可复用 Prompt；不调用生图渠道。 |
| `generate` | 先确认，再调用选定 Provider、保存并检查结果。 |

优先采用用户指定模式；否则按“分析方案 / 给提示词 / 直接出图”推断。只有意图确实模糊时，才只问一个模式问题。

## 执行

1. 只用当前可用能力读取输入并归一化；不重复解释原始内容。
2. 默认启用 Loki。允许等价于 `--no-ip` 的明确关闭，或本任务临时使用上传 IP；仅在用户明确要求命名并保存时创建自定义品牌包。
3. 接受风格编号或名称。未指定时自动推荐；首次生成前展示紧凑的 12 风格清单，并用 `★ 推荐` 标记推荐项。
4. 用户语言选择优先；否则中文用简体中文，其他语言保持原文，不隐式翻译。
5. 默认建议 `standard`（目标约 2K），但只展示 Provider 真正支持的能力。Codex 内置渠道没有独立质量参数；把高质量意图写入 Prompt/工具调用，不伪造参数。自定义端点未声明质量能力时省略 `quality`；只有显式 capability map 才映射并展示。

## 生成授权

首次生成必须让用户明确确认：品牌/IP 状态、风格、Provider、比例、质量、图片数量、文字语言。每次实际生图（包括同任务迭代）都必须让用户明确确认当前比例。Provider、质量或图片数量实质变化后，恢复完整确认。

紧急、“使用默认”、其他任务的批准或“不要问”都不能替代确认。确认后，Codex 直接调用内置 `imagegen` 并附所需参考图，绝不从 Python 调用；APIMart 或自定义端点才运行 `scripts/loki_image2.py generate`。APIMart 使用本地参考图时必须显式传 `--reference-image-file`；`--brand` 绝不自动注入图片。密钥只从环境变量读取，绝不放进参数或聊天。

生成后检查内容准确、文字可读、布局、风格一致性和 IP 锚点。报告失败，不自动重新生成，也不自动切换 Provider。POST 结果不明时保留 `ambiguous_submission`/计费未知语义，要求先核账且不重投。首版拒绝视频；缺少必要读取、转录或视觉能力时说明限制，不推断内容。

## 按需读取

- 涉及模式、确认、比例、迭代、成本或输出时，读 [工作流](references/workflow.md)。
- 需要归一化文本、文档、音频或图片时，读 [输入归一化](references/input-normalization.md)。
- 选择 Loki、临时 IP 或持久品牌时，读 [品牌包 Schema](references/brand-pack-schema.md)。
- 选择或推荐风格时，读 [12 套风格](references/styles.md)。
- 组装或质检最终 Prompt 时，读 [Prompt 系统](references/prompt-system.md)。
- 选择渠道、映射质量、运行 CLI 或处理失败时，读 [Provider](references/providers.md)。

## 红旗

- 听到“使用默认”就直接生成。
- 推定比例，或沿用上一任务的确认。
- 质检失败后自动重生成或 fallback。
- 在命令、错误、日志或聊天中出现密钥。
