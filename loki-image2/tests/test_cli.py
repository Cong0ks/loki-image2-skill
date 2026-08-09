from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import base64
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_ROOT / "scripts" / "loki_image2.py"
sys.path.insert(0, str(SKILL_ROOT))

from scripts import common
from scripts.providers.apimart import (
    AmbiguousSubmissionError,
    GenerationResult,
    ProviderError,
)
from scripts.providers.openai_compatible import ImagePayload

try:
    from scripts import loki_image2 as cli
except ImportError:
    cli = None


TEST_TEMP_ROOT = SKILL_ROOT / ".test-tmp"
TEST_TEMP_ROOT.mkdir(exist_ok=True)
PNG = b"\x89PNG\r\n\x1a\nminimal-payload"
JPEG = b"\xff\xd8\xffminimal-payload"


@contextmanager
def writable_temporary_directory():
    original_mkdir = os.mkdir

    def mkdir_with_workspace_acl(path, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            return original_mkdir(path, 0o777)
        return original_mkdir(path, 0o777, dir_fd=dir_fd)

    with patch.object(tempfile._os, "mkdir", side_effect=mkdir_with_workspace_acl):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            yield Path(directory)


@contextmanager
def changed_directory(path: Path):
    original = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(original)


@contextmanager
def redirected_path_resolution(source: Path, target: Path):
    """Simulate a directory link when Windows symlink creation is unavailable."""
    original_resolve = Path.resolve
    resolved_source = original_resolve(source)
    resolved_target = original_resolve(target)

    def resolve_with_redirect(path, *args, **kwargs):
        resolved = original_resolve(path, *args, **kwargs)
        try:
            relative = resolved.relative_to(resolved_source)
        except ValueError:
            return resolved
        return resolved_target / relative

    with patch.object(
        Path,
        "resolve",
        autospec=True,
        side_effect=resolve_with_redirect,
    ):
        yield


def invoke(argv: list[str]) -> tuple[int, dict[str, object], str, str]:
    if cli is None:
        raise AssertionError("scripts.loki_image2 is not importable")
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(argv)
    stream = stdout.getvalue() if code == 0 else stderr.getvalue()
    payload = json.loads(stream)
    return code, payload, stdout.getvalue(), stderr.getvalue()


class FakeAPIMartClient:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeOpenAIClient(FakeAPIMartClient):
    pass


class UnifiedCLITests(unittest.TestCase):
    def setUp(self):
        if cli is None:
            self.fail("scripts.loki_image2 is not importable")
        self.default_home_patcher = patch.object(
            Path,
            "home",
            side_effect=AssertionError("tests must not access the default runtime root"),
        )
        self.default_home = self.default_home_patcher.start()
        self.addCleanup(self.default_home_patcher.stop)

    def tearDown(self):
        self.default_home.assert_not_called()

    def write_prompt(self, root: Path, value: str = "draw a tiny ghost") -> Path:
        path = root / "prompt.md"
        path.write_text(value, encoding="utf-8")
        return path

    def test_providers_subprocess_works_from_another_directory_without_reading_keys(self):
        with writable_temporary_directory() as temp:
            environment = os.environ.copy()
            environment["LOKI_IMAGE_HOME"] = str(temp / "runtime")
            environment["APIMART_API_KEY"] = "must-not-appear"
            environment["CUSTOM_IMAGE_API_KEY"] = "must-not-appear-either"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT.resolve()), "providers"],
                cwd=temp,
                env=environment,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertEqual(len(completed.stdout.splitlines()), 1)
        payload = json.loads(completed.stdout)
        self.assertEqual([item["id"] for item in payload["providers"]], [
            "codex", "apimart", "openai-compatible",
        ])
        self.assertNotIn("must-not-appear", completed.stdout + completed.stderr)

    def test_providers_direct_does_not_query_provider_key_environment(self):
        queries: list[str] = []

        with writable_temporary_directory() as temp:
            def guarded_get(name, default=None):
                queries.append(name)
                if name in {"APIMART_API_KEY", "CUSTOM_IMAGE_API_KEY"}:
                    raise AssertionError("providers must not read provider credentials")
                if name == "LOKI_IMAGE_HOME":
                    return str(temp / "runtime")
                return default

            with patch.object(cli.os.environ, "get", side_effect=guarded_get):
                code, payload, stdout, stderr = invoke(["providers"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertTrue(payload["ok"])
        self.assertNotIn("APIMART_API_KEY", queries)
        self.assertNotIn("CUSTOM_IMAGE_API_KEY", queries)

    def test_successful_cli_start_cleans_existing_logs_by_age(self):
        with writable_temporary_directory() as temp:
            home = temp / "runtime"
            logs = home / "logs"
            logs.mkdir(parents=True)
            old_log = logs / "old.log"
            recent_log = logs / "recent.log"
            old_log.write_text("old", encoding="utf-8")
            recent_log.write_text("recent", encoding="utf-8")
            now = time.time()
            os.utime(old_log, (now - 8 * 24 * 60 * 60, now - 8 * 24 * 60 * 60))
            os.utime(recent_log, (now - 6 * 24 * 60 * 60, now - 6 * 24 * 60 * 60))

            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                code, payload, stdout, stderr = invoke(["providers"])

            self.assertEqual(code, 0, stderr)
            self.assertTrue(payload["ok"])
            self.assertFalse(old_log.exists())
            self.assertTrue(recent_log.exists())

    def test_cli_skips_log_directory_resolving_outside_runtime_root(self):
        with writable_temporary_directory() as temp:
            home = temp / "runtime"
            logs = home / "logs"
            outside = temp / "outside-logs"
            logs.mkdir(parents=True)
            outside.mkdir()
            inside_old = logs / "inside-old.log"
            outside_old = outside / "outside-old.log"
            for path in (inside_old, outside_old):
                path.write_text("must survive", encoding="utf-8")
                old = time.time() - 8 * 24 * 60 * 60
                os.utime(path, (old, old))

            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                with redirected_path_resolution(logs, outside):
                    code, payload, stdout, stderr = invoke(["providers"])

            self.assertEqual(code, 0, stderr)
            self.assertTrue(payload["ok"])
            self.assertTrue(inside_old.exists())
            self.assertTrue(outside_old.exists())

    def test_cli_skips_log_directory_marked_as_reparse_point(self):
        with writable_temporary_directory() as temp:
            home = temp / "runtime"
            logs = home / "logs"
            logs.mkdir(parents=True)
            old_log = logs / "old.log"
            old_log.write_text("must survive", encoding="utf-8")
            old = time.time() - 8 * 24 * 60 * 60
            os.utime(old_log, (old, old))
            original = common._is_reparse_point

            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                with patch.object(
                    common,
                    "_is_reparse_point",
                    side_effect=lambda path: Path(path) == logs or original(path),
                ):
                    code, payload, stdout, stderr = invoke(["providers"])

            self.assertEqual(code, 0, stderr)
            self.assertTrue(payload["ok"])
            self.assertTrue(old_log.exists())

    def test_startup_log_cleanup_failure_does_not_block_safe_commands_or_write_logs(self):
        with writable_temporary_directory() as temp:
            home = temp / "runtime"
            logs = home / "logs"
            logs.mkdir(parents=True)
            sentinel = logs / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            prompt = self.write_prompt(temp)

            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                with patch.object(cli, "cleanup_old_logs", side_effect=OSError("denied")):
                    providers = invoke(["providers"])
                    dry_run = invoke([
                        "dry-run", "--provider", "apimart",
                        "--prompt-file", str(prompt), "--ratio", "1:1",
                        "--quality", "standard", "--count", "1",
                    ])

            self.assertEqual(providers[0], 0, providers[3])
            self.assertEqual(dry_run[0], 0, dry_run[3])
            self.assertEqual(list(logs.iterdir()), [sentinel])

    def test_successful_providers_and_dry_run_do_not_create_missing_log_directory(self):
        with writable_temporary_directory() as temp:
            home = temp / "runtime"
            prompt = self.write_prompt(temp)
            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                providers = invoke(["providers"])
                dry_run = invoke([
                    "dry-run", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1",
                ])

            self.assertEqual(providers[0], 0, providers[3])
            self.assertEqual(dry_run[0], 0, dry_run[3])
            self.assertFalse((home / "logs").exists())

    def test_subprocess_failure_has_only_utf8_json_stderr_and_hides_argv_secret(self):
        fake_secret = "sk" + "-subprocess-secret"
        with writable_temporary_directory() as temp:
            environment = os.environ.copy()
            environment["LOKI_IMAGE_HOME"] = str(temp / "runtime")
            completed = subprocess.run(
                [sys.executable, str(SCRIPT.resolve()), "providers", "--api-key", fake_secret],
                cwd=temp,
                env=environment,
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(len(completed.stderr.splitlines()), 1)
        payload = json.loads(completed.stderr)
        self.assertFalse(payload["ok"])
        self.assertNotIn(fake_secret, completed.stderr)

    def test_help_subcommand_is_json_and_never_echoes_extra_argv(self):
        with writable_temporary_directory() as temp:
            environment = os.environ.copy()
            environment["LOKI_IMAGE_HOME"] = str(temp / "runtime")
            help_result = subprocess.run(
                [sys.executable, str(SCRIPT.resolve()), "help"],
                cwd=temp,
                env=environment,
                capture_output=True,
                check=False,
            )
            secret = "sk" + "-help-argv-must-not-echo"
            bad_result = subprocess.run(
                [sys.executable, str(SCRIPT.resolve()), "help", secret],
                cwd=temp,
                env=environment,
                capture_output=True,
                check=False,
            )

        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertEqual(help_result.stderr, b"")
        payload = json.loads(help_result.stdout.decode("utf-8"))
        self.assertEqual(payload["commands"], [
            "providers", "brand", "dry-run", "generate", "help",
        ])
        self.assertEqual(bad_result.returncode, 2)
        self.assertNotIn(secret.encode("utf-8"), bad_result.stdout + bad_result.stderr)

    def test_help_flag_returns_the_same_safe_json_as_help_subcommand(self):
        with writable_temporary_directory() as temp:
            with patch.dict(
                os.environ,
                {"LOKI_IMAGE_HOME": str(temp / "runtime")},
                clear=True,
            ):
                command_help = invoke(["help"])
                flag_help = invoke(["--help"])

        self.assertEqual(command_help[0], 0, command_help[3])
        self.assertEqual(flag_help[0], 0, flag_help[3])
        self.assertEqual(flag_help[1], command_help[1])
        self.assertEqual(flag_help[2], command_help[2])
        self.assertEqual(flag_help[3], "")

    def test_subprocess_forces_non_ascii_json_to_raw_utf8_bytes(self):
        with writable_temporary_directory() as temp:
            prompt = self.write_prompt(temp)
            environment = os.environ.copy()
            environment["LOKI_IMAGE_HOME"] = str(temp / "runtime")
            environment["PYTHONIOENCODING"] = "ascii:strict"
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT.resolve()), "dry-run",
                    "--provider", "openai-compatible",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1",
                    "--base-url", "https://images.example/v1", "--model", "模型-一号",
                ],
                cwd=temp,
                env=environment,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, b"")
        decoded = completed.stdout.decode("utf-8")
        self.assertIn("模型-一号", decoded)
        self.assertEqual(json.loads(decoded)["model"], "模型-一号")

    def test_dry_run_maps_quality_without_key_prompt_or_side_effects(self):
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp, "private final prompt")
            home = temp / "runtime"
            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                code, payload, stdout, stderr = invoke([
                    "dry-run", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "16:9",
                    "--quality", "high", "--count", "2",
                ])

            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mapped_quality"], "4k")
            self.assertEqual(payload["prompt_characters"], 20)
            self.assertNotIn("private final prompt", stdout + stderr)
            self.assertFalse((temp / "output").exists())
            self.assertFalse(home.exists())

    def test_custom_dry_run_omits_undeclared_quality_and_maps_explicit_capability(self):
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp)
            home = temp / "runtime"
            base = [
                "dry-run", "--provider", "openai-compatible",
                "--prompt-file", str(prompt), "--ratio", "1:1",
                "--quality", "standard", "--count", "1",
                "--base-url", "https://images.example/v1", "--model", "model",
            ]
            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                omitted = invoke(base)
                mapped = invoke(base + [
                    "--custom-quality-map",
                    "draft=low,standard=balanced,high=ultra",
                ])

            self.assertEqual(omitted[0], 0, omitted[3])
            self.assertIsNone(omitted[1]["mapped_quality"])
            self.assertEqual(omitted[1]["quality_parameter"], "omitted")
            self.assertEqual(mapped[0], 0, mapped[3])
            self.assertEqual(mapped[1]["mapped_quality"], "balanced")
            self.assertEqual(mapped[1]["quality_parameter"], "declared-map")
            self.assertFalse(home.exists())
            self.assertFalse((temp / "output").exists())

    def test_apimart_dry_run_rejects_malformed_base_port_without_side_effects(self):
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp)
            home = temp / "runtime"
            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                code, payload, stdout, stderr = invoke([
                    "dry-run", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1",
                    "--base-url", "https://example.test:broken/v1",
                ])

            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertFalse(payload["ok"])
            self.assertFalse(home.exists())
            self.assertFalse((temp / "output").exists())

    def test_api_key_option_is_unknown_and_never_echoes_its_value(self):
        fake_secret = "sk" + "-fake-cli-secret"
        with writable_temporary_directory() as temp:
            prompt = self.write_prompt(temp)
            with patch.dict(os.environ, {
                "LOKI_IMAGE_HOME": str(temp / "runtime"),
            }, clear=True):
                code, payload, stdout, stderr = invoke([
                    "generate", "--confirmed", "--provider", "openai-compatible",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1", "--no-ip",
                    "--base-url", "https://images.example/v1", "--model", "model",
                    "--api-key", fake_secret,
                ])

        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_type"], "usage_error")
        self.assertNotIn(fake_secret, stdout + stderr)

    def test_generate_confirmation_and_ratio_gates_precede_provider_creation(self):
        with writable_temporary_directory() as temp:
            prompt = self.write_prompt(temp)
            constructor = Mock()
            with patch.dict(os.environ, {
                "LOKI_IMAGE_HOME": str(temp / "runtime"),
            }, clear=True), patch.object(cli, "APIMartClient", constructor):
                first = invoke([
                    "generate", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1",
                ])
                second = invoke([
                    "generate", "--confirmed", "--provider", "apimart",
                    "--prompt-file", str(prompt),
                    "--quality", "standard", "--count", "1",
                ])

            self.assertEqual(first[0], 2)
            self.assertEqual(second[0], 2)
            constructor.assert_not_called()

    def test_missing_provider_key_is_redacted_in_stderr_and_single_error_log(self):
        secret = "sk" + "-secret-never-show"
        base64_value = "QWxhZGRpbjpPcGVuU2VzYW1lMTIzNDU2Nzg5MA=="
        prompt_text = f"private prompt {secret} {base64_value}"
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp, prompt_text)
            home = temp / "runtime"
            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                code, payload, stdout, stderr = invoke([
                    "generate", "--confirmed", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1",
                    "--no-ip",
                ])

            logs = list((home / "logs").glob("*.log"))
            combined = stdout + stderr + "".join(path.read_text(encoding="utf-8") for path in logs)
            self.assertEqual(code, 2)
            self.assertFalse(payload["ok"])
            self.assertEqual(len(logs), 1)
            for sensitive in (secret, base64_value, prompt_text, str(home)):
                self.assertNotIn(sensitive, combined)

    def test_missing_custom_key_is_redacted_without_transport_or_output(self):
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt_text = "private custom prompt"
            prompt = self.write_prompt(temp, prompt_text)
            home = temp / "runtime"
            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                code, payload, stdout, stderr = invoke([
                    "generate", "--confirmed", "--provider", "openai-compatible",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1", "--no-ip",
                    "--base-url", "https://images.example/v1", "--model", "model",
                ])

            logs = list((home / "logs").glob("*.log"))
            combined = stdout + stderr + "".join(path.read_text(encoding="utf-8") for path in logs)
            self.assertEqual(code, 2)
            self.assertFalse(payload["ok"])
            self.assertEqual(len(logs), 1)
            self.assertFalse((temp / "output").exists())
            self.assertNotIn(prompt_text, combined)
            self.assertNotIn(str(home), combined)

    def test_brand_add_show_and_list_round_trip_in_injected_home(self):
        with writable_temporary_directory() as temp:
            home = temp / "runtime"
            image = temp / "hero.png"
            image.write_bytes(PNG)
            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                added = invoke([
                    "brand", "add", "My Hero", "--image", str(image),
                    "--display-name", "Hero Display",
                ])
                shown = invoke(["brand", "show", "my-hero"])
                listed = invoke(["brand", "list"])

            self.assertEqual(added[0], 0)
            self.assertEqual(shown[0], 0)
            self.assertEqual(shown[1]["brand"]["display_name"], "Hero Display")
            self.assertEqual(shown[1]["brand"]["source"], "user")
            self.assertEqual(
                [item["id"] for item in listed[1]["brands"]],
                ["loki", "my-hero"],
            )
            self.assertNotIn("character_image", listed[1]["brands"][1])

    def test_missing_named_brand_and_mutually_exclusive_brand_flags_fail_before_transport(self):
        with writable_temporary_directory() as temp:
            prompt = self.write_prompt(temp)
            constructor = Mock()
            with patch.dict(os.environ, {
                "LOKI_IMAGE_HOME": str(temp / "runtime"),
            }, clear=True), patch.object(cli, "APIMartClient", constructor):
                missing = invoke([
                    "generate", "--confirmed", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1",
                    "--brand", "does-not-exist",
                ])
                conflict = invoke([
                    "generate", "--confirmed", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1",
                    "--brand", "loki", "--no-ip",
                ])

            self.assertEqual(missing[0], 2)
            self.assertEqual(conflict[0], 2)
            constructor.assert_not_called()

    def test_apimart_success_downloads_and_saves_complete_artifacts_without_fallback(self):
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp, "final prompt")
            home = temp / "runtime"
            fake = FakeAPIMartClient(GenerationResult(
                task_id="remote-task", image_urls=("https://cdn.example/a.png",),
            ))

            def fake_download(url, destination, **kwargs):
                output = Path(destination).with_suffix(".png")
                output.write_bytes(PNG)
                return output

            with patch.dict(os.environ, {
                "LOKI_IMAGE_HOME": str(home), "APIMART_API_KEY": "key-not-metadata",
            }, clear=True), patch.object(cli, "APIMartClient", return_value=fake), \
                    patch.object(cli, "OpenAICompatibleClient") as other, \
                    patch.object(cli, "download_image", side_effect=fake_download):
                code, payload, stdout, stderr = invoke([
                    "generate", "--confirmed", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "16:9",
                    "--quality", "high", "--count", "1",
                    "--style", "chalk", "--topic", "Ghost Lesson",
                ])

            self.assertEqual(code, 0, stderr)
            other.assert_not_called()
            output = Path(payload["output_dir"])
            metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual((output / "prompt.md").read_text(encoding="utf-8"), "final prompt")
            self.assertEqual(metadata["provider"], "apimart")
            self.assertEqual(metadata["task_id"], "remote-task")
            self.assertEqual(metadata["brand"], "loki")
            self.assertNotIn("key-not-metadata", json.dumps(metadata))
            self.assertEqual(len(fake.requests), 1)
            self.assertEqual(tuple(fake.requests[0].reference_images), ())

    def test_apimart_reference_image_file_is_bounded_validated_and_converted_in_process(self):
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp)
            reference = temp / "reference.untrusted-extension"
            reference.write_bytes(PNG)
            fake = FakeAPIMartClient(GenerationResult(
                task_id="remote-task", image_urls=("https://cdn.example/a.png",),
            ))

            def fake_download(url, destination, **kwargs):
                output = Path(destination).with_suffix(".png")
                output.write_bytes(PNG)
                return output

            with patch.dict(os.environ, {
                "LOKI_IMAGE_HOME": str(temp / "runtime"),
                "APIMART_API_KEY": "key",
            }, clear=True), patch.object(cli, "APIMartClient", return_value=fake), \
                    patch.object(cli, "download_image", side_effect=fake_download):
                result = invoke([
                    "generate", "--confirmed", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1", "--no-ip",
                    "--reference-image-file", str(reference),
                ])

            self.assertEqual(result[0], 0, result[3])
            expected = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
            self.assertEqual(tuple(fake.requests[0].reference_images), (expected,))
            self.assertNotIn(expected, result[2] + result[3])

    def test_reference_image_file_rejects_bad_signature_and_conflicts_with_url(self):
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp)
            reference = temp / "fake.png"
            reference.write_bytes(b"not-an-image")
            home = temp / "runtime"
            base = [
                "dry-run", "--provider", "apimart", "--prompt-file", str(prompt),
                "--ratio", "1:1", "--quality", "standard", "--count", "1",
            ]
            with patch.dict(os.environ, {"LOKI_IMAGE_HOME": str(home)}, clear=True):
                bad_signature = invoke(base + [
                    "--reference-image-file", str(reference),
                ])
                conflict = invoke(base + [
                    "--reference-image", "https://example.test/reference.png",
                    "--reference-image-file", str(reference),
                ])

            self.assertEqual(bad_signature[0], 2)
            self.assertEqual(bad_signature[1]["error_type"], "validation_error")
            self.assertEqual(conflict[0], 2)
            self.assertEqual(conflict[1]["error_type"], "usage_error")
            self.assertFalse(home.exists())
            self.assertFalse((temp / "output").exists())

    def test_reference_image_file_enforces_bounded_read_limit(self):
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp)
            reference = temp / "reference.png"
            reference.write_bytes(PNG)
            with patch.dict(os.environ, {
                "LOKI_IMAGE_HOME": str(temp / "runtime"),
            }, clear=True), patch.object(cli, "MAX_REFERENCE_IMAGE_BYTES", 8):
                result = invoke([
                    "dry-run", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1",
                    "--reference-image-file", str(reference),
                ])

            self.assertEqual(result[0], 2)
            self.assertEqual(result[1]["error_type"], "validation_error")
            self.assertFalse((temp / "output").exists())

    def test_reference_image_secure_open_accepts_png_jpeg_and_webp(self):
        formats = (
            ("image/png", PNG),
            ("image/jpeg", JPEG),
            ("image/webp", b"RIFF\x10\x00\x00\x00WEBPminimal-payload"),
        )
        for media_type, image_bytes in formats:
            with self.subTest(media_type=media_type), writable_temporary_directory() as temp:
                reference = temp / "reference.bin"
                reference.write_bytes(image_bytes)

                result = cli._reference_image_data_uri(str(reference))

                prefix, encoded = result.split(",", 1)
                self.assertEqual(prefix, f"data:{media_type};base64")
                self.assertEqual(base64.b64decode(encoded), image_bytes)

    def test_reference_image_file_rejects_replacement_between_validation_and_open(self):
        with writable_temporary_directory() as temp:
            reference = temp / "reference.png"
            reference.write_bytes(PNG)
            original_lstat = Path.lstat
            replaced = False

            def lstat_then_replace(path, *args, **kwargs):
                nonlocal replaced
                details = original_lstat(path, *args, **kwargs)
                if Path(path) == reference and not replaced:
                    reference.unlink()
                    reference.write_bytes(JPEG)
                    replaced = True
                return details

            with patch.object(
                Path,
                "lstat",
                autospec=True,
                side_effect=lstat_then_replace,
            ):
                with self.assertRaises(cli.CLIError):
                    cli._reference_image_data_uri(str(reference))

            self.assertTrue(replaced)

    def test_openai_url_and_base64_success_save_signature_validated_files(self):
        cases = (
            ("url", (ImagePayload(url="https://cdn.example/result.jpg"),), JPEG),
            ("base64", (ImagePayload(data=PNG),), PNG),
        )
        for name, result, downloaded in cases:
            with self.subTest(name=name), writable_temporary_directory() as temp, changed_directory(temp):
                prompt = self.write_prompt(temp)
                fake = FakeOpenAIClient(result)

                def fake_download(url, destination, **kwargs):
                    output = Path(destination).with_suffix(".jpg")
                    output.write_bytes(downloaded)
                    return output

                with patch.dict(os.environ, {
                    "LOKI_IMAGE_HOME": str(temp / "runtime"),
                    "CUSTOM_IMAGE_API_KEY": "custom-secret",
                }, clear=True), patch.object(cli, "OpenAICompatibleClient", return_value=fake), \
                        patch.object(cli, "APIMartClient") as other, \
                        patch.object(cli, "download_image", side_effect=fake_download):
                    code, payload, stdout, stderr = invoke([
                        "generate", "--confirmed", "--provider", "openai-compatible",
                        "--prompt-file", str(prompt), "--ratio", "1:1",
                        "--quality", "draft", "--count", "1", "--no-ip",
                        "--base-url", "https://images.example/v1", "--model", "custom-model",
                    ])

                self.assertEqual(code, 0, stderr)
                other.assert_not_called()
                output = Path(payload["output_dir"])
                metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
                image_path = output / metadata["output_files"][0]
                self.assertTrue(image_path.read_bytes().startswith(downloaded[:8]))
                self.assertEqual(metadata["model"], "custom-model")
                self.assertNotIn("custom-secret", json.dumps(metadata))

    def test_loopback_download_requires_flag_and_remote_http_is_never_approved(self):
        urls = (
            ("http://localhost/image.png", False, 2),
            ("http://localhost/image.png", True, 0),
            ("http://example.test/image.png", True, 2),
        )
        for url, allowed, expected in urls:
            with self.subTest(url=url, allowed=allowed), writable_temporary_directory() as temp, changed_directory(temp):
                prompt = self.write_prompt(temp)
                fake = FakeOpenAIClient((ImagePayload(url=url),))
                response = Mock()
                response.headers = {"Content-Type": "image/png"}
                response.read = Mock(side_effect=[PNG, b""])
                response.close = Mock()

                def real_download(candidate, destination, **kwargs):
                    return common.download_image(
                        candidate, destination, opener=Mock(return_value=response), **kwargs
                    )

                argv = [
                    "generate", "--confirmed", "--provider", "openai-compatible",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1", "--no-ip",
                    "--base-url", "https://images.example/v1", "--model", "model",
                ]
                if allowed:
                    argv.append("--allow-local-http")
                with patch.dict(os.environ, {
                    "LOKI_IMAGE_HOME": str(temp / "runtime"), "CUSTOM_IMAGE_API_KEY": "key",
                }, clear=True), patch.object(cli, "OpenAICompatibleClient", return_value=fake), \
                        patch.object(cli, "download_image", side_effect=real_download):
                    result = invoke(argv)

                self.assertEqual(result[0], expected)

    def test_provider_failure_logs_once_leaves_no_output_and_never_falls_back(self):
        secret = "sk" + "-provider-secret"
        prompt_text = "prompt that must be absent"
        encoded = "QWxhZGRpbjpPcGVuU2VzYW1lMTIzNDU2Nzg5MA=="
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp, prompt_text)
            home = temp / "runtime"
            fake = FakeAPIMartClient(ProviderError(f"failure {secret} {prompt_text} {encoded}"))
            with patch.dict(os.environ, {
                "LOKI_IMAGE_HOME": str(home), "APIMART_API_KEY": secret,
            }, clear=True), patch.object(cli, "APIMartClient", return_value=fake), \
                    patch.object(cli, "OpenAICompatibleClient") as other:
                code, payload, stdout, stderr = invoke([
                    "generate", "--confirmed", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1", "--no-ip",
                ])

            logs = list((home / "logs").glob("*.log"))
            combined = stdout + stderr + "".join(path.read_text(encoding="utf-8") for path in logs)
            self.assertEqual(code, 2)
            self.assertEqual(len(fake.requests), 1)
            self.assertEqual(len(logs), 1)
            self.assertFalse((temp / "output").exists())
            other.assert_not_called()
            for value in (secret, prompt_text, encoded, str(home)):
                self.assertNotIn(value, combined)

    def test_ambiguous_submission_has_billing_safe_machine_fields_and_no_fallback(self):
        prompt_text = "sensitive prompt that must stay private"
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp, prompt_text)
            home = temp / "runtime"
            fake = FakeAPIMartClient(AmbiguousSubmissionError("POST outcome unknown"))
            with patch.dict(os.environ, {
                "LOKI_IMAGE_HOME": str(home), "APIMART_API_KEY": "private-key",
            }, clear=True), patch.object(cli, "APIMartClient", return_value=fake), \
                    patch.object(cli, "OpenAICompatibleClient") as other:
                code, payload, stdout, stderr = invoke([
                    "generate", "--confirmed", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1", "--no-ip",
                ])

            logs = list((home / "logs").glob("*.log"))
            combined = stdout + stderr + "".join(
                path.read_text(encoding="utf-8") for path in logs
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["code"], "ambiguous_submission")
            self.assertIs(payload["billing_unknown"], True)
            self.assertIs(payload["retryable"], False)
            self.assertIn("核查", payload["message"])
            self.assertIn("不重投", payload["message"])
            self.assertEqual(len(fake.requests), 1)
            self.assertEqual(len(logs), 1)
            other.assert_not_called()
            self.assertFalse((temp / "output").exists())
            self.assertNotIn(prompt_text, combined)
            self.assertNotIn("private-key", combined)

    def test_download_failure_logs_once_cleans_staging_and_leaves_no_output(self):
        with writable_temporary_directory() as temp, changed_directory(temp):
            prompt = self.write_prompt(temp)
            home = temp / "runtime"
            fake = FakeAPIMartClient(GenerationResult(
                task_id="remote-task", image_urls=("https://cdn.example/a.png",),
            ))
            with patch.dict(os.environ, {
                "LOKI_IMAGE_HOME": str(home), "APIMART_API_KEY": "key",
            }, clear=True), patch.object(cli, "APIMartClient", return_value=fake), \
                    patch.object(cli, "download_image", side_effect=OSError("download failed")):
                code, payload, stdout, stderr = invoke([
                    "generate", "--confirmed", "--provider", "apimart",
                    "--prompt-file", str(prompt), "--ratio", "1:1",
                    "--quality", "standard", "--count", "1", "--no-ip",
                ])

            self.assertEqual(code, 2)
            self.assertEqual(len(list((home / "logs").glob("*.log"))), 1)
            self.assertFalse((temp / "output").exists())
            self.assertEqual(list(temp.glob(".loki-image2-*")), [])


if __name__ == "__main__":
    unittest.main()
