from __future__ import annotations

import re
from pathlib import Path
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = SKILL_ROOT / "SKILL.md"
OPENAI_YAML = SKILL_ROOT / "agents" / "openai.yaml"

EXPECTED_DESCRIPTION = (
    "Use when a user wants to turn text, documents, audio, or images into a "
    "branded infographic plan, reusable image prompt, or generated image using "
    "Loki, a custom IP character, Codex imagegen, APIMart, or an OpenAI "
    "Images-compatible endpoint."
)
REFERENCE_FILES = {
    "references/workflow.md",
    "references/prompt-system.md",
    "references/styles.md",
    "references/providers.md",
    "references/brand-pack-schema.md",
    "references/input-normalization.md",
}
STYLE_NAMES = (
    "Loki Whiteboard Pro",
    "Loki Blackboard Kids",
    "Loki Blackboard Pro",
    "Loki Friendly Illustration",
    "Loki Retro Vector",
    "Loki Clean Report",
    "Loki Dark Tech",
    "Loki Journal Watercolor",
    "Loki Chinese Classic",
    "Loki AI Blueprint",
    "Loki Future Editorial",
    "Loki Clay Lab",
)
NORMALIZED_FIELDS = (
    "source_type",
    "source_language",
    "topic",
    "audience",
    "purpose",
    "facts",
    "hierarchy",
    "verbatim_text",
    "visual_cues",
    "constraints",
)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise AssertionError("SKILL.md must start with YAML frontmatter")
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError("SKILL.md frontmatter is not closed") from exc
    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise AssertionError(f"Unsupported frontmatter line: {line!r}")
        if key.strip() in metadata:
            raise AssertionError(f"Duplicate frontmatter field: {key.strip()}")
        metadata[key.strip()] = value.strip().strip('"')
    return metadata, "\n".join(lines[closing + 1 :])


def parse_interface_yaml(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    in_interface = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            if line.strip() != "interface:":
                raise AssertionError(f"Unexpected top-level YAML field: {line!r}")
            in_interface = line.strip() == "interface:"
            continue
        if not in_interface or not line.startswith("  "):
            continue
        key, separator, raw_value = line.strip().partition(":")
        if not separator:
            raise AssertionError(f"Unsupported interface line: {line!r}")
        raw_value = raw_value.strip()
        if len(raw_value) < 2 or raw_value[0] != '"' or raw_value[-1] != '"':
            raise AssertionError(f"Interface strings must be quoted: {line!r}")
        values[key] = raw_value[1:-1]
    return values


def markdown_section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)",
        text,
    )
    if match is None:
        raise AssertionError(f"Missing Markdown section: {heading}")
    return match.group(1)


def fenced_blocks(text: str, language: str | None = None) -> list[str]:
    language_pattern = re.escape(language) if language is not None else r"[^\n]*"
    return re.findall(
        rf"(?ms)^```{language_pattern}\s*$\n(.*?)^```\s*$",
        text,
    )


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = SKILL_PATH.read_text(encoding="utf-8")
        cls.metadata, cls.body = parse_frontmatter(cls.skill)

    def read_reference(self, relative_path: str) -> str:
        path = SKILL_ROOT / relative_path
        self.assertTrue(path.is_file(), f"missing required reference: {relative_path}")
        return path.read_text(encoding="utf-8")

    def all_documentation(self) -> str:
        chunks = [self.skill]
        for relative_path in sorted(REFERENCE_FILES):
            path = SKILL_ROOT / relative_path
            if path.is_file():
                chunks.append(path.read_text(encoding="utf-8"))
        return "\n".join(chunks)

    def test_frontmatter_is_exact_and_discoverable(self):
        self.assertEqual(set(self.metadata), {"name", "description"})
        self.assertEqual(self.metadata["name"], "loki-image2")
        self.assertEqual(self.metadata["description"], EXPECTED_DESCRIPTION)
        self.assertTrue(self.metadata["description"].startswith("Use when"))
        self.assertLess(len(self.metadata["description"]), 500)
        frontmatter = self.skill.split("---", 2)[1]
        self.assertLess(len(frontmatter), 1024)
        self.assertLess(len(self.skill.splitlines()), 500)
        self.assertLess(len(re.findall(r"\S+", self.skill)), 500)

    def test_six_direct_reference_links_exist_and_resolve(self):
        matches = re.findall(r"\[[^\]]+\]\((references/[^)]+\.md)\)", self.body)
        links = set(matches)
        self.assertEqual(len(matches), 6)
        self.assertEqual(links, REFERENCE_FILES)
        for relative_path in links:
            self.assertTrue((SKILL_ROOT / relative_path).is_file(), relative_path)

    def test_three_modes_have_explicit_stopping_boundaries(self):
        workflow = self.read_reference("references/workflow.md")
        self.assertRegex(workflow, r"(?s)`plan`.{0,300}不生成最终 Prompt.{0,120}不生图")
        self.assertRegex(workflow, r"(?s)`prompt`.{0,300}最终.{0,80}Prompt.{0,120}不调用.{0,40}生图")
        self.assertRegex(workflow, r"(?s)`generate`.{0,400}确认.{0,160}生成.{0,160}保存.{0,160}检查")

    def test_generation_confirmation_contract_closes_baseline_loopholes(self):
        workflow = self.read_reference("references/workflow.md")
        confirmation = markdown_section(workflow, "生成确认")
        templates = fenced_blocks(confirmation, "text")
        self.assertGreaterEqual(len(templates), 2)
        first_confirmation = templates[0]
        for field in ("品牌/IP 状态", "风格", "Provider", "比例", "质量", "图片数量", "文字语言"):
            self.assertIn(field, first_confirmation)
        contract = self.skill + "\n" + workflow
        self.assertIn("每次同任务迭代", confirmation)
        iteration_confirmation = templates[1]
        self.assertIn("当前比例", iteration_confirmation)
        self.assertRegex(iteration_confirmation, r"(必须明确确认|明确确认.{0,20}比例)")
        for loophole in ("紧急", "使用默认", "不要问"):
            self.assertIn(loophole, confirmation)
        self.assertRegex(confirmation, r"(过去任务|其他任务|prior.task).{0,80}(不能|不得|不).{0,30}(满足|绕过|替代|继承).{0,30}确认")
        self.assertRegex(confirmation, r"(紧急|使用默认|不要问).{0,180}(不能|不得|不).{0,30}(满足|绕过|替代).{0,30}确认")
        iteration = markdown_section(workflow, "任务与迭代")
        self.assertRegex(iteration, r"Provider、质量或图片数量.{0,40}(变化|实质变化).{0,30}恢复完整确认")
        self.assertRegex(contract, r"不自动(重新生成|重生成)")
        self.assertRegex(contract, r"不自动(切换|回退|fallback)")

    def test_first_confirmation_quality_is_provider_aware(self):
        workflow = self.read_reference("references/workflow.md")
        confirmation = markdown_section(workflow, "生成确认")
        first_confirmation = fenced_blocks(confirmation, "text")[0]
        self.assertIn("只显示所选 Provider 对应的一行", first_confirmation)
        self.assertNotIn("质量：draft / standard / high", first_confirmation)
        self.assertRegex(first_confirmation, r"Codex.{0,40}高质量意图.{0,40}无独立.{0,20}quality.{0,20}参数")
        for mapping in ("draft→1k", "standard→2k", "high→4k"):
            self.assertIn(mapping, first_confirmation)
        self.assertRegex(
            first_confirmation,
            r"openai-compatible.{0,100}(未声明|没有声明).{0,80}(省略|不发送).{0,20}quality",
        )
        self.assertRegex(
            first_confirmation,
            r"openai-compatible.{0,120}(已声明|显式).{0,80}(映射|quality map)",
        )

    def test_styles_are_numbered_once_and_support_recommendation(self):
        styles = self.read_reference("references/styles.md")
        for number, name in enumerate(STYLE_NAMES, start=1):
            self.assertEqual(styles.count(name), 1, name)
            self.assertRegex(styles, rf"(?m)^##\s+{number}\.\s+{re.escape(name)}$")
        self.assertIn("推荐矩阵", styles)
        workflow = self.read_reference("references/workflow.md")
        self.assertRegex(workflow, r"(自动推荐|推荐理由)")
        self.assertRegex(workflow, r"(★|推荐标记|标记为推荐)")

    def test_input_normalization_schema_and_supported_inputs(self):
        normalization = self.read_reference("references/input-normalization.md")
        for field in NORMALIZED_FIELDS:
            self.assertIn(f"`{field}`", normalization)
        for source_class in ("文本", "主流文档", "音频", "图片"):
            self.assertIn(source_class, normalization)
        self.assertRegex(normalization, r"视频.{0,20}(不支持|拒绝)")
        self.assertRegex(normalization, r"观察.{0,80}推断")
        self.assertRegex(normalization, r"(无法读取|能力缺失|工具缺失).{0,80}(停止|不推断|不得推断)")

    def test_brand_defaults_ip_off_and_persistence_contract(self):
        brand = self.read_reference("references/brand-pack-schema.md")
        self.assertIn("assets/brands/loki/", brand)
        self.assertIn("~/.codex/loki-image/brands/<brand-name>/", brand)
        self.assertRegex(brand, r"Loki.{0,30}默认")
        self.assertIn("--no-ip", brand)
        self.assertRegex(brand, r"明确.{0,30}(命名|名称).{0,50}(保存|持久化)")
        self.assertRegex(brand, r"不.{0,20}静默覆盖")
        self.assertRegex(brand, r'"schema_version"\s*:\s*1')
        for field in ("id", "display_name", "character_image", "anchors", "default_palette"):
            self.assertIn(f'"{field}"', brand)
        for command in ("brand list", "brand show", "brand add"):
            self.assertIn(command, brand)
        examples = fenced_blocks(markdown_section(brand, "CLI 示例"), "bash")
        self.assertEqual(len(examples), 1)
        command_lines = [line for line in examples[0].splitlines() if line.startswith("python ")]
        self.assertEqual(len(command_lines), 3)
        for command_line in command_lines:
            self.assertTrue(
                command_line.startswith('python "<skill-root>/scripts/loki_image2.py" '),
                command_line,
            )
        self.assertNotRegex(brand, r"(?<![\w-])--api-key(?!-env)\b")

    def test_provider_contracts_match_implemented_capabilities(self):
        providers = self.read_reference("references/providers.md")
        codex = markdown_section(providers, "Codex 内置 imagegen")
        apimart = markdown_section(providers, "APIMart")
        custom = markdown_section(providers, "自定义 OpenAI Images 兼容端点")
        self.assertIn("APIMART_API_KEY", providers)
        self.assertIn("CUSTOM_IMAGE_API_KEY", providers)
        self.assertIn("https://api.apimart.ai/v1/images/generations", providers)
        self.assertIn("https://api.apimart.ai/v1/tasks/{task_id}", providers)
        self.assertIn("gpt-image-2", providers)
        for mapping in ("draft→1k", "standard→2k", "high→4k"):
            self.assertIn(mapping, providers)
        self.assertIn("--custom-quality-map", custom)
        self.assertRegex(custom, r"(未提供|未声明).{0,80}(省略|不发送).{0,20}`?quality`?")
        self.assertRegex(custom, r"draft=low,standard=medium,high=high")
        self.assertIn("image_urls", providers)
        self.assertIn("b64_json", providers)
        self.assertRegex(codex, r"(直接调用|直接使用).{0,40}`?imagegen`?")
        self.assertRegex(codex, r"(不暴露|没有).{0,40}(独立)?质量.{0,20}(flag|参数)")
        self.assertRegex(apimart, r"pending.{0,40}submitted.{0,40}processing.{0,80}completed.{0,80}failed.{0,40}cancelled")
        self.assertIn("data.result.images[].url[]", apimart)
        self.assertRegex(apimart, r"GET.{0,80}(退避)?重试")
        self.assertRegex(apimart, r"(POST.{0,80}(不|不得)重试|(不|不得)重试.{0,80}POST)")
        self.assertRegex(custom, r"首版不支持参考图")
        self.assertRegex(providers, r"(真实冒烟测试|live smoke test).{0,180}(Provider|渠道).{0,80}比例.{0,80}质量.{0,80}(数量|图片数量).{0,80}(费用|成本)")
        cli_examples = markdown_section(providers, "CLI 示例")
        command_lines = [
            line
            for block in fenced_blocks(cli_examples, "bash")
            for line in block.splitlines()
            if line.startswith("python ")
        ]
        self.assertGreaterEqual(len(command_lines), 3)
        for command_line in command_lines:
            self.assertTrue(
                command_line.startswith('python "<skill-root>/scripts/loki_image2.py" '),
                command_line,
            )
        for command in ("providers", "dry-run", "generate"):
            self.assertTrue(any(f" {command}" in line for line in command_lines), command)
        generate_lines = [line for line in command_lines if " generate " in line]
        self.assertTrue(generate_lines)
        self.assertTrue(all("--confirmed" in line for line in generate_lines))
        self.assertNotRegex(providers, r"(?<![\w-])--api-key(?!-env)\b")

    def test_provider_docs_cover_file_reference_ambiguous_submission_and_http_boundaries(self):
        providers = self.read_reference("references/providers.md")
        workflow = self.read_reference("references/workflow.md")
        self.assertIn("--reference-image-file", providers)
        self.assertRegex(providers, r"--brand loki.{0,120}(不会|不得|不).{0,30}(自动|隐式).{0,40}(注入|参考图)")
        for field in (
            "ambiguous_submission", "billing_unknown", "retryable",
        ):
            self.assertIn(field, providers)
        self.assertRegex(providers, r"(最终 URL|response\.geturl\(\)).{0,160}(校验|验证)")
        self.assertRegex(providers, r"(PNG|JPEG).{0,40}WebP.{0,100}(magic|签名)")
        self.assertRegex(providers, r"JSON.{0,60}(有界|上限)")
        self.assertRegex(providers, r"X-Amz-.{0,80}X-Goog-")
        self.assertRegex(workflow, r"(唯一|不复用|不覆盖).{0,50}(任务目录|输出目录)")
        self.assertIn(" help", providers)
        self.assertRegex(providers, r"stdout/stderr.{0,60}UTF-8")

    def test_output_and_redacted_log_contract(self):
        workflow = self.read_reference("references/workflow.md")
        save_section = markdown_section(workflow, "保存与检查")
        self.assertIn("output/loki-image2/", save_section)
        self.assertIn("prompt.md", save_section)
        self.assertIn("metadata.json", save_section)
        self.assertIn("~/.codex/loki-image/logs/", save_section)
        self.assertRegex(save_section, r"参数解析成功后.{0,60}安全的.{0,30}日志目录.{0,30}(存在|已存在).{0,80}清理.{0,40}(超过|早于)\s*7\s*日")
        self.assertRegex(save_section, r"写(入)?错误日志.{0,80}清理.{0,40}(超过|早于)\s*7\s*日")
        self.assertIn("脱敏", save_section)

        providers = self.read_reference("references/providers.md")
        failure_section = markdown_section(providers, "失败、日志与 live 门禁")
        self.assertIn("~/.codex/loki-image/logs/", failure_section)
        self.assertRegex(failure_section, r"参数解析成功后.{0,60}安全的.{0,30}日志目录.{0,30}(存在|已存在).{0,80}清理.{0,40}(超过|早于)\s*7\s*日")
        self.assertRegex(failure_section, r"写(入)?错误日志.{0,80}清理.{0,40}(超过|早于)\s*7\s*日")

    def test_openai_yaml_exact_utf8_interface(self):
        raw = OPENAI_YAML.read_bytes()
        text = raw.decode("utf-8")
        values = parse_interface_yaml(text)
        self.assertEqual(
            values,
            {
                "display_name": "Loki Image2",
                "short_description": "将内容转为可控的品牌信息图并支持多渠道生图",
                "default_prompt": (
                    "Use $loki-image2 to turn this content into a branded infographic "
                    "plan or prompt, and ask before generating an image."
                ),
            },
        )


if __name__ == "__main__":
    unittest.main()
