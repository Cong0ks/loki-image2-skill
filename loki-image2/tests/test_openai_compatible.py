from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from scripts.providers.apimart import (
    AmbiguousSubmissionError,
    GenerationRequest,
    ProviderError,
    RetryableTransportError,
)
from scripts.providers.openai_compatible import (
    ImagePayload,
    OpenAICompatibleClient,
    validate_base_url,
)


FIXTURES = Path(__file__).with_name("fixtures")
PNG_BYTES = b"\x89PNG\r\n\x1a\nloki-image2-test"


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RecordingTransport:
    def __init__(self, *responses: object):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def __call__(self, method, url, *, headers, payload=None, timeout=30.0):
        self.calls.append({
            "method": method,
            "url": url,
            "headers": dict(headers),
            "payload": payload,
            "timeout": timeout,
        })
        if not self.responses:
            raise AssertionError("unexpected transport call")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeHTTPResponse:
    def __init__(self, body: bytes, *, status: int = 200):
        self.body = body
        self.status = status

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class OpenAICompatibleClientTests(unittest.TestCase):
    def client(self, transport, **kwargs):
        return OpenAICompatibleClient(
            "https://images.example.test/v1",
            "custom-image-model",
            transport=transport,
            **kwargs,
        )

    def generate(self, client, request=None, *, key="environment-test-key"):
        request = request or GenerationRequest(prompt="tiny ghost", ratio="16:9")
        with patch.dict(os.environ, {"CUSTOM_IMAGE_API_KEY": key}, clear=True):
            return client.generate(request)

    def test_url_fixture_returns_url_payload_without_bytes(self):
        transport = RecordingTransport(fixture("openai_url.json"))

        result = self.generate(self.client(transport))

        self.assertEqual(
            result,
            (ImagePayload(url="https://example.test/generated.png", data=None),),
        )
        self.assertEqual(len(transport.calls), 1)

    def test_base64_fixture_strictly_decodes_png_payload_without_url(self):
        transport = RecordingTransport(fixture("openai_b64.json"))

        result = self.generate(self.client(transport))

        self.assertEqual(result, (ImagePayload(url=None, data=PNG_BYTES),))
        self.assertTrue(result[0].data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_default_transport_accepts_standard_openai_envelope(self):
        captured = {}

        def fake_urlopen(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            body = json.dumps(fixture("openai_url.json")).encode("utf-8")
            return FakeHTTPResponse(body)

        with patch.dict(
            os.environ,
            {"CUSTOM_IMAGE_API_KEY": "environment-test-key"},
            clear=True,
        ):
            with patch("scripts.providers.apimart.urlopen", side_effect=fake_urlopen):
                result = OpenAICompatibleClient(
                    "https://images.example.test/v1",
                    "custom-image-model",
                ).generate(GenerationRequest(prompt="tiny ghost", ratio="16:9"))

        self.assertEqual(
            result,
            (ImagePayload(url="https://example.test/generated.png", data=None),),
        )
        request = captured["request"]
        self.assertEqual(request.full_url, "https://images.example.test/v1/images/generations")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(captured["timeout"], 30.0)

    def test_remote_http_is_rejected_even_with_local_http_approval(self):
        for base_url in (
            "http://example.test/v1",
            "http://192.0.2.1/v1",
            "http://localhost.example.test/v1",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    validate_base_url(base_url, allow_local_http=True)

    def test_loopback_http_requires_approval_for_each_exact_host(self):
        cases = (
            ("http://localhost:8080/v1", "http://localhost:8080/v1/images/generations"),
            ("http://127.0.0.1/v1", "http://127.0.0.1/v1/images/generations"),
            ("http://[::1]/v1", "http://[::1]/v1/images/generations"),
        )
        for base_url, endpoint in cases:
            with self.subTest(base_url=base_url, approved=False):
                with self.assertRaises(ValueError):
                    validate_base_url(base_url)
            with self.subTest(base_url=base_url, approved=True):
                transport = RecordingTransport(fixture("openai_url.json"))
                client = OpenAICompatibleClient(
                    base_url,
                    "model",
                    allow_local_http=True,
                    transport=transport,
                )
                self.generate(client)
                self.assertEqual(transport.calls[0]["url"], endpoint)

    def test_https_endpoint_joining_preserves_v1_and_never_duplicates_suffix(self):
        cases = (
            ("https://example.test", "https://example.test/images/generations"),
            ("https://example.test/", "https://example.test/images/generations"),
            ("https://example.test/v1", "https://example.test/v1/images/generations"),
            ("https://example.test/v1/", "https://example.test/v1/images/generations"),
            (
                "https://example.test/v1/images/generations",
                "https://example.test/v1/images/generations",
            ),
            (
                "https://example.test/v1/images/generations/",
                "https://example.test/v1/images/generations",
            ),
        )
        for base_url, endpoint in cases:
            with self.subTest(base_url=base_url):
                transport = RecordingTransport(fixture("openai_url.json"))
                client = OpenAICompatibleClient(base_url, "model", transport=transport)
                self.generate(client)
                self.assertEqual(transport.calls[0]["url"], endpoint)

    def test_unsafe_and_malformed_base_urls_raise_value_error(self):
        invalid = (
            "https://user:pass@example.test/v1",
            "https://example.test/v1?api_key=secret",
            "https://example.test/v1?",
            "https://example.test/v1#fragment",
            "https://example.test/v1#",
            "https://[broken/v1",
            "https://example.test:bad/v1",
            "ftp://example.test/v1",
            "https://exa mple.test/v1",
            "",
        )
        for base_url in invalid:
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    validate_base_url(base_url)

    def test_constructor_rejects_empty_model_and_invalid_env_var_name(self):
        for model in ("", " ", None):
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    OpenAICompatibleClient("https://example.test/v1", model)
        for env_name in ("", "1KEY", "CUSTOM-KEY", "CUSTOM KEY", None):
            with self.subTest(env_name=env_name):
                with self.assertRaises(ValueError):
                    OpenAICompatibleClient(
                        "https://example.test/v1",
                        "model",
                        api_key_env=env_name,
                    )

    def test_missing_or_blank_environment_key_fails_before_transport(self):
        for environment in ({}, {"CUSTOM_IMAGE_API_KEY": "   "}):
            with self.subTest(environment=environment):
                transport = RecordingTransport()
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ProviderError, "CUSTOM_IMAGE_API_KEY"):
                        self.client(transport).generate(
                            GenerationRequest(prompt="secret prompt", ratio="1:1")
                        )
                self.assertEqual(transport.calls, [])

    def test_post_contains_exact_headers_body_with_explicit_quality_mapping(self):
        for quality, mapped in (
            ("draft", "low"),
            ("standard", "medium"),
            ("high", "high"),
        ):
            with self.subTest(quality=quality):
                transport = RecordingTransport(fixture("openai_url.json"))
                request = GenerationRequest(
                    prompt="draw a tiny ghost",
                    ratio="1536x1024",
                    quality=quality,
                    count=2,
                )
                self.generate(self.client(
                    transport,
                    quality_map={
                        "draft": "low",
                        "standard": "medium",
                        "high": "high",
                    },
                ), request, key="env-only-key")
                self.assertEqual(transport.calls, [{
                    "method": "POST",
                    "url": "https://images.example.test/v1/images/generations",
                    "headers": {
                        "Authorization": "Bearer " + "env-only-key",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    "payload": {
                        "model": "custom-image-model",
                        "prompt": "draw a tiny ghost",
                        "n": 2,
                        "size": "1536x1024",
                        "quality": mapped,
                    },
                    "timeout": 30.0,
                }])

    def test_undeclared_quality_capability_omits_quality_parameter(self):
        for quality in ("draft", "standard", "high"):
            with self.subTest(quality=quality):
                transport = RecordingTransport(fixture("openai_url.json"))

                self.generate(
                    self.client(transport),
                    GenerationRequest(prompt="ghost", ratio="1:1", quality=quality),
                )

                self.assertNotIn("quality", transport.calls[0]["payload"])

    def test_custom_quality_map_is_used_and_unsupported_quality_fails_early(self):
        transport = RecordingTransport(fixture("openai_url.json"))
        client = self.client(transport, quality_map={"standard": "balanced"})

        self.generate(client)

        self.assertEqual(transport.calls[0]["payload"]["quality"], "balanced")

        unsupported_transport = RecordingTransport()
        unsupported = self.client(
            unsupported_transport,
            quality_map={"standard": "balanced"},
        )
        with patch.dict(os.environ, {"CUSTOM_IMAGE_API_KEY": "key"}, clear=True):
            with self.assertRaises(ValueError):
                unsupported.generate(
                    GenerationRequest(prompt="ghost", ratio="1:1", quality="high")
                )
        self.assertEqual(unsupported_transport.calls, [])

    def test_reference_images_raise_provider_error_before_transport(self):
        transport = RecordingTransport()
        request = GenerationRequest(
            prompt="ghost",
            ratio="1:1",
            reference_images=("https://example.test/reference.png",),
        )

        with patch.dict(os.environ, {"CUSTOM_IMAGE_API_KEY": "key"}, clear=True):
            with self.assertRaises(ProviderError):
                self.client(transport).generate(request)

        self.assertEqual(transport.calls, [])

    def test_invalid_request_fields_fail_before_transport(self):
        invalid = (
            GenerationRequest(prompt="", ratio="1:1"),
            GenerationRequest(prompt="ghost", ratio=" "),
            GenerationRequest(prompt="ghost", ratio="1:1", count=0),
            GenerationRequest(prompt="ghost", ratio="1:1", count=True),
        )
        for request in invalid:
            with self.subTest(request=request):
                transport = RecordingTransport()
                with patch.dict(os.environ, {"CUSTOM_IMAGE_API_KEY": "key"}, clear=True):
                    with self.assertRaises(ValueError):
                        self.client(transport).generate(request)
                self.assertEqual(transport.calls, [])

    def test_malformed_or_empty_data_is_rejected(self):
        malformed = (
            [],
            {},
            {"data": None},
            {"data": {}},
            {"data": []},
            {"error": {"message": "provider detail must not be copied"}},
        )
        for response in malformed:
            with self.subTest(response=response):
                with self.assertRaises(ProviderError):
                    self.generate(self.client(RecordingTransport(response)))

    def test_malformed_result_items_are_rejected(self):
        malformed_items = (
            "https://example.test/generated.png",
            {},
            {"url": "https://example.test/a.png", "b64_json": "YWJj"},
            {"url": ""},
            {"url": 123},
            {"b64_json": ""},
            {"b64_json": 123},
        )
        for item in malformed_items:
            with self.subTest(item=item):
                with self.assertRaises(ProviderError):
                    self.generate(self.client(RecordingTransport({"data": [item]})))

    def test_invalid_and_oversized_base64_are_rejected_without_leaking_data(self):
        cases = (
            ("not!strict!base64", None),
            ("YWJjZA==", 3),
        )
        for encoded, max_bytes in cases:
            with self.subTest(encoded=encoded, max_bytes=max_bytes):
                transport = RecordingTransport({"data": [{"b64_json": encoded}]})
                context = (
                    patch("scripts.providers.openai_compatible.MAX_DOWNLOAD_BYTES", max_bytes)
                    if max_bytes is not None
                    else patch("scripts.providers.openai_compatible.MAX_DOWNLOAD_BYTES", 25 * 1024 * 1024)
                )
                with context:
                    with self.assertRaises(ProviderError) as raised:
                        self.generate(self.client(transport))
                self.assertNotIn(encoded, str(raised.exception))

    def test_result_urls_enforce_https_or_approved_exact_loopback_http(self):
        unsafe = (
            "http://example.test/generated.png",
            "http://localhost.example.test/generated.png",
            "https://user:pass@example.test/generated.png",
            "https://[broken/generated.png",
            "https://exa mple.test/generated.png",
        )
        for url in unsafe:
            with self.subTest(url=url):
                transport = RecordingTransport({"data": [{"url": url}]})
                with self.assertRaises(ProviderError):
                    self.generate(self.client(transport))

        for base_url, result_url in (
            ("http://localhost/v1", "http://localhost/generated.png"),
            ("http://127.0.0.1/v1", "http://127.0.0.1/generated.png"),
            ("http://[::1]/v1", "http://[::1]/generated.png"),
        ):
            with self.subTest(result_url=result_url):
                transport = RecordingTransport({"data": [{"url": result_url}]})
                client = OpenAICompatibleClient(
                    base_url,
                    "model",
                    allow_local_http=True,
                    transport=transport,
                )
                self.assertEqual(
                    self.generate(client),
                    (ImagePayload(url=result_url, data=None),),
                )

    def test_transport_failure_is_redacted_suppressed_and_never_retried(self):
        key = "secret-key-never-show"
        prompt = "this complete prompt must never show"
        base64_token = "QWxhZGRpbjpPcGVuU2VzYW1lMTIzNDU2Nzg5MA=="
        transport = RecordingTransport(
            RuntimeError(f"transport exposed {key} {prompt} {base64_token}")
        )

        with self.assertRaises(ProviderError) as raised:
            self.generate(
                self.client(transport),
                GenerationRequest(prompt=prompt, ratio="1:1"),
                key=key,
            )

        message = str(raised.exception)
        self.assertNotIn(key, message)
        self.assertNotIn(prompt, message)
        self.assertNotIn(base64_token, message)
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertEqual(len(transport.calls), 1)

    def test_retryable_post_transport_failure_becomes_ambiguous_without_retry(self):
        transport = RecordingTransport(RetryableTransportError("connection lost"))

        with self.assertRaises(AmbiguousSubmissionError) as raised:
            self.generate(self.client(transport))

        self.assertIn("not retried", str(raised.exception))
        self.assertEqual(len(transport.calls), 1)


if __name__ == "__main__":
    unittest.main()
