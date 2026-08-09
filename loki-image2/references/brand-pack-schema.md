# 品牌包与 Schema

## 位置与选择优先级

- 内置 Loki：`assets/brands/loki/`，默认启用。
- 用户品牌根：`~/.codex/loki-image/brands/<brand-name>/`。
- 加载顺序：用户明确选择 > 本任务临时上传 > Loki 默认。
- 用户明确关闭 IP（等价 `--no-ip`）时覆盖全部选择，不加载角色图。

临时上传只服务当前任务，不自动写入品牌目录。只有用户同时表达明确名称和保存/持久化意图时，才创建用户品牌；不得根据文件名猜测保存意图。

## `brand.json` schema version 1

```json
{
  "schema_version": 1,
  "id": "my-brand",
  "display_name": "My Brand",
  "character_image": "character.png",
  "anchors": ["stable visual anchor"],
  "default_palette": ["#FFFFFF", "#161616"]
}
```

| 字段 | 约束 |
|---|---|
| `schema_version` | 必须为整数 `1` |
| `id` | 由品牌名安全规范化后的 ID，且必须与目录名一致 |
| `display_name` | 非空显示名称 |
| `character_image` | 仅文件名；支持 `.png`、`.jpg`/`.jpeg`、`.webp` |
| `anchors` | 字符串数组，可为空 |
| `default_palette` | 字符串数组，可为空 |

内置 Loki 文件另带 `"enabled_by_default": true` 作为包内声明；用户品牌运行时 schema 不依赖该扩展字段，默认选择由工作流决定。

## 安全与保存

- 把名称规范化为小写安全 ID；拒绝绝对路径、盘符、`..`、斜杠、反斜杠和目录逃逸。
- `character_image` 必须是单一文件名，并用 PNG、JPEG 或 WebP 文件签名验证扩展名，不能只信 MIME 或后缀。
- 不在品牌包保存 API Key、环境变量值、Authorization、完整 Prompt 或原始输入。
- 同名品牌存在时不静默覆盖。先请用户改名；只有用户明确确认覆盖后才可使用 `--overwrite`。

## CLI 示例

先把 `<skill-root>` 解析为当前 `SKILL.md` 所在目录的绝对路径。以下命令可从任意工作目录执行，只管理本地品牌元数据与图片，不包含任何 key 数据：

```bash
python "<skill-root>/scripts/loki_image2.py" brand list
python "<skill-root>/scripts/loki_image2.py" brand show loki
python "<skill-root>/scripts/loki_image2.py" brand add my-brand --image ./character.png --display-name "My Brand"
```
