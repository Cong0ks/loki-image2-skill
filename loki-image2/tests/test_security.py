from pathlib import Path
import sys

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from datetime import datetime, timedelta
from contextlib import contextmanager
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from scripts import common
from scripts.common import (
    MAX_DOWNLOAD_BYTES,
    cleanup_old_logs,
    download_image,
    ensure_within,
    redact_text,
    safe_slug,
    save_task_artifacts,
    write_error_log,
)


_TEST_TEMP_ROOT = SKILL_ROOT / ".test-tmp"
_TEST_TEMP_ROOT.mkdir(exist_ok=True)
FIXTURES = Path(__file__).with_name("fixtures")
PNG_BYTES = b"\x89PNG\r\n\x1a\nloki-image2-test"
JPEG_BYTES = b"\xff\xd8\xffloki-image2-test"
WEBP_BYTES = b"RIFF\x10\x00\x00\x00WEBPloki-image2-test"


@contextmanager
def writable_temporary_directory():
    """Use TemporaryDirectory while avoiding this Windows sandbox's 0700 ACL bug."""
    original_mkdir = os.mkdir

    def mkdir_with_workspace_acl(path, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            return original_mkdir(path, 0o777)
        return original_mkdir(path, 0o777, dir_fd=dir_fd)

    with unittest.mock.patch.object(tempfile._os, "mkdir", side_effect=mkdir_with_workspace_acl):
        with tempfile.TemporaryDirectory(dir=_TEST_TEMP_ROOT) as directory:
            yield directory


@contextmanager
def redirected_path_resolution(source: Path, target: Path):
    """Simulate an unavailable Windows symlink by redirecting Path.resolve()."""
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

    with unittest.mock.patch.object(
        Path,
        "resolve",
        autospec=True,
        side_effect=resolve_with_redirect,
    ):
        yield


class FakeResponse:
    def __init__(self, content_type, chunks, *, final_url=None, status=200, location=None):
        self.headers = {"Content-Type": content_type}
        if location is not None:
            self.headers["Location"] = location
        self._chunks = list(chunks)
        self.read_sizes = []
        self.final_url = final_url
        self.status = status
        self.closed = False

    def read(self, size):
        self.read_sizes.append(size)
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True

    def geturl(self):
        return self.final_url


class SecurityPrimitiveTests(unittest.TestCase):
    def test_safe_slug_rejects_path_values_and_keeps_unicode_words_safe(self):
        for value in ("../evil", "..\\evil", "C:\\evil", "C:evil", "/tmp/evil"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safe_slug(value)

        slug = safe_slug("Héllo, 世界!")
        self.assertEqual(slug, "hello-世界")
        self.assertEqual(slug, slug.lower())
        self.assertNotIn("/", slug)
        self.assertNotIn("\\", slug)

    def test_ensure_within_accepts_descendant_and_rejects_resolved_escape(self):
        with writable_temporary_directory() as temp:
            root = Path(temp) / "root"
            root.mkdir()
            descendant = root / "nested" / "file.txt"
            self.assertEqual(ensure_within(root, root), root.resolve())
            self.assertEqual(ensure_within(root, descendant), descendant.resolve())

            with self.assertRaises(ValueError):
                ensure_within(root, root / "nested" / ".." / ".." / "outside.txt")

    def test_redact_text_removes_credentials_and_preserves_safe_query_value(self):
        value = "Authorization: Bearer sk-secret\nhttps://x.test/?x=1&token=abc"
        redacted = redact_text(value)

        self.assertNotIn("sk-secret", redacted)
        self.assertNotIn("token=abc", redacted)
        self.assertIn("x=1", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redact_text_independently_masks_token_shapes_and_sensitive_query_keys(self):
        value = (
            "Bearer " + "bearer-secret " + "sk" + "-direct-secret "
            "https://x.test/?TOKEN=one&key=two&Api_Key=three&apikey=four"
            "&signature=five&SIG=six&x=1"
        )

        redacted = redact_text(value)

        for secret in (
            "bearer-secret", "sk" + "-direct-secret", "one", "two", "three",
            "four", "five", "six",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, redacted)
        self.assertIn("x=1", redacted)

    def test_redact_text_masks_the_entire_authorization_header_value(self):
        value = "Authorization: secret https://user:password@example.test/private"

        redacted = redact_text(value)

        self.assertEqual(redacted, "Authorization: [REDACTED]")

    def test_cloud_signed_url_fixture_is_fully_redacted_but_safe_query_remains(self):
        urls = json.loads(
            (FIXTURES / "signed_image_urls.json").read_text(encoding="utf-8")
        )

        for provider, url in urls.items():
            with self.subTest(provider=provider):
                redacted = redact_text(url)
                for secret in (
                    "AKIAFAKE", "aws-signature-secret", "fake-service",
                    "google-signature-secret", "azure-signature-secret",
                    "cloudfront-policy-secret", "cloudfront-signature-secret",
                    "KFAKE123",
                ):
                    self.assertNotIn(secret, redacted)
                self.assertIn("safe=1", redacted)
                self.assertIn("[REDACTED]", redacted)

    def test_cleanup_old_logs_observes_strict_seven_day_cutoff(self):
        with writable_temporary_directory() as temp:
            log_dir = Path(temp)
            now = datetime(2026, 8, 9, 12, 0, 0)
            old = log_dir / "old.log"
            recent = log_dir / "recent.log"
            cutoff = log_dir / "cutoff.log"
            for path, age in ((old, 8), (recent, 6), (cutoff, 7)):
                path.write_text("log", encoding="utf-8")
                timestamp = (now - timedelta(days=age)).timestamp()
                os.utime(path, (timestamp, timestamp))

            self.assertEqual(cleanup_old_logs(log_dir, now=now), 1)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(cutoff.exists())

    def test_cleanup_old_logs_rejects_reparse_directory_without_traversal(self):
        with writable_temporary_directory() as temp:
            log_dir = Path(temp) / "logs"
            log_dir.mkdir()
            old_log = log_dir / "outside-old.log"
            old_log.write_text("must survive", encoding="utf-8")
            now = datetime(2026, 8, 9, 12, 0, 0)
            timestamp = (now - timedelta(days=8)).timestamp()
            os.utime(old_log, (timestamp, timestamp))
            original = common._is_reparse_point

            with unittest.mock.patch.object(
                common,
                "_is_reparse_point",
                side_effect=lambda path: Path(path) == log_dir or original(path),
            ):
                with self.assertRaises(ValueError):
                    cleanup_old_logs(log_dir, now=now)

            self.assertTrue(old_log.exists())

    def test_cleanup_old_logs_skips_individual_reparse_log_file(self):
        with writable_temporary_directory() as temp:
            log_dir = Path(temp)
            now = datetime(2026, 8, 9, 12, 0, 0)
            regular = log_dir / "regular.log"
            reparse = log_dir / "reparse.log"
            for path in (regular, reparse):
                path.write_text("old", encoding="utf-8")
                timestamp = (now - timedelta(days=8)).timestamp()
                os.utime(path, (timestamp, timestamp))
            original = common._is_reparse_point

            with unittest.mock.patch.object(
                common,
                "_is_reparse_point",
                side_effect=lambda path: Path(path) == reparse or original(path),
            ):
                self.assertEqual(cleanup_old_logs(log_dir, now=now), 1)

            self.assertFalse(regular.exists())
            self.assertTrue(reparse.exists())

    def test_write_error_log_rejects_reparse_directory_without_external_write(self):
        with writable_temporary_directory() as temp:
            log_dir = Path(temp) / "logs"
            log_dir.mkdir()
            sentinel = log_dir / "sentinel.txt"
            sentinel.write_text("must survive", encoding="utf-8")
            original = common._is_reparse_point

            with unittest.mock.patch.object(
                common,
                "_is_reparse_point",
                side_effect=lambda path: Path(path) == log_dir or original(path),
            ):
                with self.assertRaises(ValueError):
                    write_error_log(
                        log_dir,
                        error_type="unsafe",
                        provider="provider",
                        summary="must not write",
                        now=datetime(2026, 8, 9, 12, 0, 0),
                    )

            self.assertEqual(list(log_dir.iterdir()), [sentinel])

    def test_write_error_log_has_only_allowed_fields_and_redacts_secret(self):
        with writable_temporary_directory() as temp:
            log_dir = Path(temp) / "nested" / "logs"
            log_dir.mkdir(parents=True)
            old_log = log_dir / "old.log"
            old_log.write_text("old", encoding="utf-8")
            old_timestamp = datetime(2026, 8, 1, 11, 59, 59).timestamp()
            os.utime(old_log, (old_timestamp, old_timestamp))
            log = write_error_log(
                log_dir,
                error_type="remote_error " + "sk" + "-error-secret",
                provider="provider " + "sk" + "-provider-secret",
                summary="https://x.test/?token=summary-secret&x=1",
                task_id="task sk-task-secret",
                correlation_id="correlation " + "sk" + "-correlation-secret",
                now=datetime(2026, 8, 9, 12, 0, 0),
            )
            payload = json.loads(log.read_text(encoding="utf-8"))

            self.assertEqual(
                set(payload),
                {
                    "timestamp", "error_type", "provider", "http_status",
                    "task_id", "correlation_id", "summary",
                },
            )
            serialized = log.read_text(encoding="utf-8")
            for secret in (
                "sk" + "-error-secret", "sk" + "-provider-secret", "summary-secret",
                "sk" + "-task-secret", "sk" + "-correlation-secret",
            ):
                with self.subTest(secret=secret):
                    self.assertNotIn(secret, serialized)
            self.assertIn("x=1", serialized)
            self.assertEqual(len(serialized.splitlines()), 1)
            self.assertFalse(old_log.exists())

    def test_save_task_artifacts_copies_image_writes_prompt_and_filters_metadata(self):
        with writable_temporary_directory() as temp:
            project_dir = Path(temp)
            source = project_dir / "source.png"
            source.write_bytes(b"image bytes")
            output = save_task_artifacts(
                project_dir,
                topic="Hello 世界",
                prompt="Final prompt only",
                metadata={
                    "brand": "loki", "style": "chalk", "api_key": "sk-secret",
                    "authorization": "Bearer secret", "prompt": "wrong", "raw_input": "wrong",
                    "created_at": "caller-controlled", "output_files": ["outside.png"],
                },
                images=[source],
                now=datetime(2026, 8, 9, 12, 34, 56),
            )

            self.assertEqual(output, project_dir / "output" / "loki-image2" / "20260809-123456-hello-世界")
            self.assertEqual((output / "source.png").read_bytes(), b"image bytes")
            self.assertEqual((output / "prompt.md").read_text(encoding="utf-8"), "Final prompt only")
            metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["brand"], "loki")
            self.assertNotIn("api_key", metadata)
            self.assertNotIn("authorization", metadata)
            self.assertNotIn("prompt", metadata)
            self.assertNotIn("raw_input", metadata)
            self.assertEqual(metadata["output_files"], ["source.png"])
            self.assertEqual(metadata["created_at"], "2026-08-09T12:34:56")

    def test_save_task_artifacts_atomically_creates_unique_directory_on_collision(self):
        with writable_temporary_directory() as temp:
            project_dir = Path(temp)
            timestamp = datetime(2026, 8, 9, 12, 34, 56)

            first = save_task_artifacts(
                project_dir,
                topic="same topic",
                prompt="first prompt",
                metadata={"provider": "apimart"},
                images=[],
                now=timestamp,
            )
            second = save_task_artifacts(
                project_dir,
                topic="same topic",
                prompt="second prompt",
                metadata={"provider": "apimart"},
                images=[],
                now=timestamp,
            )

            self.assertNotEqual(first, second)
            self.assertEqual((first / "prompt.md").read_text(encoding="utf-8"), "first prompt")
            self.assertEqual((second / "prompt.md").read_text(encoding="utf-8"), "second prompt")
            self.assertEqual(len(list((project_dir / "output" / "loki-image2").iterdir())), 2)

    def test_save_task_artifacts_rejects_output_ancestor_resolving_outside_project(self):
        with writable_temporary_directory() as temp:
            root = Path(temp)
            project_dir = root / "project"
            outside_dir = root / "outside"
            project_dir.mkdir()
            outside_dir.mkdir()

            with redirected_path_resolution(project_dir / "output", outside_dir):
                with self.assertRaises(ValueError):
                    save_task_artifacts(
                        project_dir,
                        topic="escape",
                        prompt="must stay inside",
                        metadata={},
                        images=[],
                        now=datetime(2026, 8, 9, 12, 34, 56),
                    )

            self.assertFalse((project_dir / "output").exists())
            self.assertEqual(list(outside_dir.iterdir()), [])

    def test_save_task_artifacts_avoids_colliding_task_with_unsafe_file_resolution(self):
        for filename in ("prompt.md", "metadata.json"):
            with self.subTest(filename=filename):
                with writable_temporary_directory() as temp:
                    root = Path(temp)
                    project_dir = root / "project"
                    outside_file = root / f"outside-{filename}"
                    task_dir = (
                        project_dir / "output" / "loki-image2"
                        / "20260809-123456-escape"
                    )
                    task_dir.mkdir(parents=True)
                    outside_file.write_text("sentinel", encoding="utf-8")

                    with redirected_path_resolution(task_dir / filename, outside_file):
                        output = save_task_artifacts(
                            project_dir,
                            topic="escape",
                            prompt="must stay inside",
                            metadata={},
                            images=[],
                            now=datetime(2026, 8, 9, 12, 34, 56),
                        )

                    self.assertEqual(outside_file.read_text(encoding="utf-8"), "sentinel")
                    self.assertNotEqual(output, task_dir)
                    self.assertEqual(
                        (output / "prompt.md").read_text(encoding="utf-8"),
                        "must stay inside",
                    )

    def test_save_task_artifacts_avoids_colliding_task_with_reparse_file_targets(self):
        for filename in ("prompt.md", "metadata.json"):
            with self.subTest(filename=filename):
                with writable_temporary_directory() as temp:
                    project_dir = Path(temp) / "project"
                    task_dir = (
                        project_dir / "output" / "loki-image2"
                        / "20260809-123456-escape"
                    )
                    task_dir.mkdir(parents=True)
                    target = task_dir / filename
                    target.write_text("sentinel", encoding="utf-8")
                    original_lstat = Path.lstat

                    def lstat_with_reparse(path, *args, **kwargs):
                        details = original_lstat(path, *args, **kwargs)
                        if Path(path) == target:
                            return SimpleNamespace(
                                st_mode=details.st_mode,
                                st_file_attributes=0x400,
                            )
                        return details

                    with unittest.mock.patch.object(
                        Path,
                        "lstat",
                        autospec=True,
                        side_effect=lstat_with_reparse,
                    ):
                        output = save_task_artifacts(
                            project_dir,
                            topic="escape",
                            prompt="must stay inside",
                            metadata={},
                            images=[],
                            now=datetime(2026, 8, 9, 12, 34, 56),
                        )

                    self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")
                    self.assertNotEqual(output, task_dir)

    def test_download_image_saves_valid_png_with_mime_derived_extension(self):
        with writable_temporary_directory() as temp:
            destination = Path(temp) / "downloaded-image"
            opener = Mock(return_value=FakeResponse(
                "image/png; charset=binary",
                [PNG_BYTES[:10], PNG_BYTES[10:]],
            ))

            output = download_image("https://example.test/image.jpg", destination, opener=opener)

            self.assertEqual(output, destination.with_suffix(".png"))
            self.assertEqual(output.read_bytes(), PNG_BYTES)
            self.assertEqual(opener.call_args.kwargs["timeout"], 30.0)
            self.assertTrue(opener.return_value.read_sizes)
            self.assertEqual(set(opener.return_value.read_sizes), {64 * 1024})

    def test_download_image_accepts_cloud_signed_https_query(self):
        signed_urls = json.loads(
            (FIXTURES / "signed_image_urls.json").read_text(encoding="utf-8")
        )
        for provider, signed_url in signed_urls.items():
            with self.subTest(provider=provider), writable_temporary_directory() as temp:
                response = FakeResponse("image/png", [PNG_BYTES])
                opener = Mock(return_value=response)

                output = download_image(signed_url, Path(temp) / "image", opener=opener)

                self.assertEqual(output.read_bytes(), PNG_BYTES)
                self.assertEqual(opener.call_args.args[0], signed_url)

    def test_download_image_accepts_jpeg_and_webp_with_mime_derived_extensions(self):
        for content_type, extension, image_bytes in (
            ("image/jpeg", ".jpg", JPEG_BYTES),
            ("image/webp", ".webp", WEBP_BYTES),
        ):
            with self.subTest(content_type=content_type):
                with writable_temporary_directory() as temp:
                    destination = Path(temp) / "downloaded-image"
                    response = FakeResponse(content_type.upper(), [image_bytes])

                    output = download_image(
                        "https://example.test/file.untrusted",
                        destination,
                        opener=Mock(return_value=response),
                    )

                    self.assertEqual(output.suffix, extension)
                    self.assertEqual(output.read_bytes(), image_bytes)

    def test_download_image_rejects_mime_signature_mismatch_without_partial(self):
        self._assert_download_failure(FakeResponse("image/png", [JPEG_BYTES]))

    def test_download_image_rejects_unsafe_final_redirect_url_and_literal_private_https(self):
        unsafe_final_urls = (
            "http://127.0.0.1/internal.png",
            "https://127.0.0.1/internal.png",
            "https://user:pass@example.test/private.png",
        )
        for final_url in unsafe_final_urls:
            with self.subTest(final_url=final_url), writable_temporary_directory() as temp:
                opener = Mock(return_value=FakeResponse(
                    "image/png", [PNG_BYTES], final_url=final_url,
                ))
                with self.assertRaises(ValueError):
                    download_image(
                        "https://cdn.example.test/image.png",
                        Path(temp) / "image",
                        opener=opener,
                    )
                opener.assert_called_once()
                self.assertEqual(list(Path(temp).iterdir()), [])

        with writable_temporary_directory() as temp:
            opener = Mock()
            with self.assertRaises(ValueError):
                download_image(
                    "https://127.0.0.1/internal.png",
                    Path(temp) / "image",
                    opener=opener,
                )
            opener.assert_not_called()

    def test_download_image_validates_each_relative_redirect_before_requesting_it(self):
        initial = "https://cdn.example.test/start/image"
        second = "https://cdn.example.test/stage/image"
        final = "https://cdn.example.test/stage/final.png"
        responses = {
            initial: FakeResponse("text/plain", [], status=302, location="../stage/image"),
            second: FakeResponse("text/plain", [], status=307, location="final.png"),
            final: FakeResponse("image/png", [PNG_BYTES]),
        }
        requested: list[str] = []

        def no_redirect_opener(url, *, timeout):
            requested.append(url)
            return responses[url]

        with writable_temporary_directory() as temp:
            try:
                output = download_image(
                    initial,
                    Path(temp) / "image",
                    opener=no_redirect_opener,
                )
            except ValueError as exc:
                self.fail(f"safe redirect chain was not followed: {exc}")

            self.assertEqual(requested, [initial, second, final])
            self.assertEqual(output.read_bytes(), PNG_BYTES)

    def test_download_image_never_requests_unsafe_redirect_location(self):
        initial = "https://cdn.example.test/image.png"
        unsafe = "http://127.0.0.1/internal.png"
        response = FakeResponse("text/plain", [], status=302, location=unsafe)
        requested: list[str] = []

        def no_redirect_opener(url, *, timeout):
            requested.append(url)
            if url != initial:
                raise AssertionError("unsafe redirect location was requested")
            return response

        with writable_temporary_directory() as temp:
            with self.assertRaises(ValueError):
                download_image(
                    initial,
                    Path(temp) / "image",
                    opener=no_redirect_opener,
                )

            self.assertEqual(requested, [initial])
            self.assertTrue(response.closed)

    def test_download_image_rejects_redirect_loop_before_repeating_request(self):
        initial = "https://cdn.example.test/image.png"
        second = "https://cdn.example.test/again.png"
        responses = {
            initial: FakeResponse("text/plain", [], status=302, location="/again.png"),
            second: FakeResponse("text/plain", [], status=301, location="/image.png"),
        }
        requested: list[str] = []

        def no_redirect_opener(url, *, timeout):
            requested.append(url)
            return responses[url]

        with writable_temporary_directory() as temp:
            with self.assertRaises(ValueError):
                download_image(
                    initial,
                    Path(temp) / "image",
                    opener=no_redirect_opener,
                )

            self.assertEqual(requested, [initial, second])

    def test_download_image_limits_redirect_chain_to_five_hops(self):
        initial = "https://cdn.example.test/0.png"
        requested: list[str] = []

        def no_redirect_opener(url, *, timeout):
            requested.append(url)
            index = int(url.rsplit("/", 1)[1].split(".", 1)[0])
            return FakeResponse(
                "text/plain",
                [],
                status=302,
                location=f"/{index + 1}.png",
            )

        with writable_temporary_directory() as temp:
            with self.assertRaises(ValueError):
                download_image(
                    initial,
                    Path(temp) / "image",
                    opener=no_redirect_opener,
                )

            self.assertEqual(
                requested,
                [f"https://cdn.example.test/{index}.png" for index in range(6)],
            )

    def test_download_image_rejects_non_https_without_calling_opener(self):
        with writable_temporary_directory() as temp:
            opener = Mock()

            with self.assertRaises(ValueError):
                download_image("http://example.test/image.png", Path(temp) / "image", opener=opener)

            opener.assert_not_called()
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_download_image_allows_only_exact_loopback_http_when_approved(self):
        accepted = (
            "http://localhost/image.png",
            "http://127.0.0.1:8080/image.png",
            "http://[::1]/image.png",
        )
        for url in accepted:
            with self.subTest(url=url):
                with writable_temporary_directory() as temp:
                    opener = Mock(return_value=FakeResponse("image/png", [PNG_BYTES]))
                    output = download_image(
                        url,
                        Path(temp) / "image",
                        opener=opener,
                        allow_local_http=True,
                    )
                    self.assertEqual(output.read_bytes(), PNG_BYTES)

        rejected = (
            "http://example.test/image.png",
            "http://localhost.example.test/image.png",
            "http://user:pass@localhost/image.png",
            "http://localhost/image.png?api_key=secret",
            "http://localhost/image.png#fragment",
            "http://localhost:broken/image.png",
            "http://[::1/image.png",
        )
        for url in rejected:
            with self.subTest(url=url):
                with writable_temporary_directory() as temp:
                    opener = Mock()
                    with self.assertRaises(ValueError):
                        download_image(
                            url,
                            Path(temp) / "image",
                            opener=opener,
                            allow_local_http=True,
                        )
                    opener.assert_not_called()

    def test_download_image_rejects_invalid_content_type_without_partial_files(self):
        self._assert_download_failure(FakeResponse("text/html", [b"not an image"]))

    def test_download_image_rejects_oversized_stream_without_partial_files(self):
        self._assert_download_failure(FakeResponse("image/png", [b"x" * (MAX_DOWNLOAD_BYTES + 1)]))

    def test_download_image_rejects_empty_body_without_partial_files(self):
        self._assert_download_failure(FakeResponse("image/png", []))

    def test_download_image_removes_partials_when_opener_fails(self):
        with writable_temporary_directory() as temp:
            destination = Path(temp) / "downloaded-image"
            with self.assertRaises(OSError):
                download_image(
                    "https://example.test/image.png",
                    destination,
                    opener=Mock(side_effect=OSError("connection failed")),
                )
            self.assertEqual(list(Path(temp).iterdir()), [])

    def _assert_download_failure(self, response):
        with writable_temporary_directory() as temp:
            destination = Path(temp) / "downloaded-image"
            with self.assertRaises(ValueError):
                download_image("https://example.test/image.png", destination, opener=Mock(return_value=response))
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
