# 工作流与授权边界

## 状态序列

严格按以下状态推进；模式边界到达后立即停止：

`normalize → choose mode → choose brand → analyze → choose/recommend style → compose prompt → confirm → generate → save → inspect`

| 模式 | 输出 | 停止边界 |
|---|---|---|
| `plan` | 主题、受众、用途、事实与层级；布局、信息密度、风格、比例和品牌建议 | 不生成最终 Prompt，也不生图。 |
| `prompt` | `plan` 全部内容，加一份最终可复用 Prompt | 不调用任何生图渠道。 |
| `generate` | Prompt、确认卡、图片、保存路径、脱敏元数据和质检结果 | 完成确认 → 生成 → 保存 → 检查；失败后停止。 |

用户已指定模式时直接使用。未指定时按“分析方案”→ `plan`、“给提示词”→ `prompt`、“直接出图”→ `generate` 推断；只有无法可靠判断时，问一次“需要方案、Prompt，还是直接生成？”

## 任务与迭代

把新的内容目标、独立交付请求或明确开启的新主题视为新任务；其他任务的批准不继承。把围绕当前已归一化内容的修改视为同任务迭代，可沿用未变化的分析、品牌和风格，但仍须执行本轮生成确认。

同任务中 Provider、质量或图片数量发生实质变化时，恢复完整确认。只修改版式、文案或细节时使用精简确认，但当前比例必须始终出现并获得明确答复。

## 比例建议

| 用途 | 建议比例 |
|---|---|
| 横版演示、视频封面 | `16:9` |
| 竖版社媒、手机海报 | `9:16` |
| 方形知识卡、头像式视觉 | `1:1` |
| 课堂、传统幻灯片 | `4:3` |
| 摄影感横图、文章头图 | `3:2` |

比例只是一项建议，不存在最终默认；每次实际生成前都确认当前比例。

## 首次风格选择

风格未指定时，根据内容、受众、用途、语气与密度自动推荐；首次生成前显示此紧凑清单，并把唯一推荐项标为 `★ 推荐`：

`1 Whiteboard Pro · 2 Blackboard Kids · 3 Blackboard Pro · 4 Friendly Illustration · 5 Retro Vector · 6 Clean Report · 7 Dark Tech · 8 Journal Watercolor · 9 Chinese Classic · 10 AI Blueprint · 11 Future Editorial · 12 Clay Lab`

同时给一句推荐理由。用户可用编号或完整名称更换。

## 生成确认

第一次生成使用以下完整模板，并等待明确同意：

```text
首次生成确认
- 品牌/IP 状态：Loki / 关闭 / 临时 IP / 已命名品牌
- 风格：编号 + 名称
- Provider：Codex / APIMart / openai-compatible
- 比例：当前值
- 质量/质量意图（只显示所选 Provider 对应的一行）：
  - 若 Provider = Codex：高质量意图，无独立 quality 参数
  - 若 Provider = APIMart：draft→1k / standard→2k / high→4k
  - 若 Provider = openai-compatible 且端点未声明质量能力：保留抽象质量意图，省略 quality 参数
  - 若 Provider = openai-compatible 且已声明显式映射：显示本端点实际 quality map
- 图片数量：N
- 文字语言：语言
请明确确认以上七项后再生成。
```

每次同任务迭代使用以下模板，并等待明确同意：

```text
迭代生成确认
- 变化项：……
- 当前 Provider：……
- 当前图片数量：……
- 当前比例：……（本次必须明确确认）
确认按以上设置生成吗？
```

紧急、“使用默认”、过去任务的确认或“不要问”都不能绕过或满足确认。`standard`（目标约 2K）、`1` 张和语言规则只能作为待确认建议。

## 成本与失败保护

- 仅对 APIMart 状态 GET 做安全重试，退避为 `1, 2, 4, 8, 15` 秒并封顶 15 秒。
- POST 提交超时或断线后结果可能已计费；不重试 POST，停止并让用户核查任务。
- Provider 失败或质检失败时只报告问题与建议；不自动 fallback、不自动重新生成。
- 真实付费调用只在本轮授权范围内执行，不用一次确认扩展数量或质量。

## 保存与检查

成功结果保存到 `<当前项目>/output/loki-image2/<YYYYMMDD-HHMMSS>-<topic>/`；目录以排他创建原子分配，碰撞时使用唯一后缀，绝不复用或覆盖旧任务目录。目录包含生成图片、`prompt.md` 和 `metadata.json`。元数据只保留品牌、风格、比例、质量、Provider、模型、时间、任务/关联 ID 和输出文件名，不保存密钥或原始输入全文。

错误日志位于 `~/.codex/loki-image/logs/` 并脱敏。参数解析成功后，若安全的 `logs` 日志目录已存在，则清理超过 7 日的 `.log` 文件；写错误日志时也先清理超过 7 日的文件。清理异常不阻断 `providers` 或 `dry-run`，不安全目录不遍历、不删除、不写入。这不是后台定时任务，目录不存在时成功命令不创建它。日志不记录完整 Prompt、原始输入、图片内容、Authorization 或密钥。保存后检查事实和数字、文字可读性、阅读顺序、布局、风格一致性及 IP 核心锚点；失败即报告并停止。
