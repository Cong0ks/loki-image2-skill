from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from scripts.providers.apimart import (
    APIMartClient,
    AmbiguousSubmissionError,
    GenerationRequest,
    PollTimeoutError,
    ProviderError,
    RetryableTransportError,
    TaskFailedError,
    request_json,
)


FIXTURES = Path(__file__).with_name("fixtures")


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RecordingTransport:
    def __init__(self, *responses):
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
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeHTTPResponse:
    def __init__(self, body: bytes, *, status: int = 200):
        self.body = body
        self.status = status
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body if size < 0 else self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class APIMartClientTests(unittest.TestCase):
    def test_create_sends_official_endpoint_headers_and_payload(self):
        transport = RecordingTransport(fixture("apimart_created.json"))
        client = APIMartClient(api_key="explicit-test-key", transport=transport)

        task_id = client.create_task(GenerationRequest(
            prompt="draw a tiny ghost",
            ratio="16:9",
            quality="standard",
            count=2,
        ))

        self.assertEqual(task_id, "task_test_001")
        self.assertEqual(len(transport.calls), 1)
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.apimart.ai/v1/images/generations")
        self.assertEqual(call["headers"], {
            "Authorization": "Bearer " + "explicit-test-key",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.assertEqual(call["payload"], {
            "model": "gpt-image-2",
            "prompt": "draw a tiny ghost",
            "n": 2,
            "size": "16:9",
            "resolution": "2k",
        })

    def test_quality_values_map_to_official_resolutions(self):
        for quality, resolution in (("draft", "1k"), ("standard", "2k"), ("high", "4k")):
            with self.subTest(quality=quality):
                transport = RecordingTransport(fixture("apimart_created.json"))
                APIMartClient(api_key="key", transport=transport).create_task(
                    GenerationRequest(prompt="ghost", ratio="1:1", quality=quality)
                )
                self.assertEqual(transport.calls[0]["payload"]["resolution"], resolution)

    def test_reference_images_are_sent_as_image_urls(self):
        references = (
            "https://example.test/reference.png",
            "data:image/webp;base64,YWJjZA==",
        )
        transport = RecordingTransport(fixture("apimart_created.json"))

        APIMartClient(api_key="key", transport=transport).create_task(
            GenerationRequest(prompt="ghost", ratio="1:1", reference_images=references)
        )

        self.assertEqual(transport.calls[0]["payload"]["image_urls"], list(references))

    def test_invalid_requests_fail_before_transport(self):
        invalid_requests = (
            GenerationRequest(prompt=" ", ratio="1:1"),
            GenerationRequest(prompt="ghost", ratio=" "),
            GenerationRequest(prompt="ghost", ratio="1:1", quality="ultra"),
            GenerationRequest(prompt="ghost", ratio="1:1", count=0),
            GenerationRequest(prompt="ghost", ratio="1:1", count=True),
            GenerationRequest(prompt="ghost", ratio="1:1", reference_images=tuple(
                f"https://example.test/{index}.png" for index in range(17)
            )),
        )
        for request in invalid_requests:
            with self.subTest(request=request):
                transport = RecordingTransport()
                with self.assertRaises(ValueError):
                    APIMartClient(api_key="key", transport=transport).create_task(request)
                self.assertEqual(transport.calls, [])

    def test_unsafe_reference_types_fail_before_transport(self):
        unsafe_values = (
            "http://example.test/reference.png",
            "https://user:password@example.test/reference.png",
            "data:text/plain;base64,YWJjZA==",
            "data:image/gif;base64,R0lGODlh",
            "   ",
            123,
        )
        for value in unsafe_values:
            with self.subTest(value=value):
                transport = RecordingTransport()
                request = GenerationRequest(
                    prompt="ghost", ratio="1:1", reference_images=(value,)
                )
                with self.assertRaises(ValueError):
                    APIMartClient(api_key="key", transport=transport).create_task(request)
                self.assertEqual(transport.calls, [])

    def test_creation_fixture_extracts_task_id_and_malformed_shapes_fail(self):
        self.assertEqual(
            APIMartClient(
                api_key="key", transport=RecordingTransport(fixture("apimart_created.json"))
            ).create_task(GenerationRequest(prompt="ghost", ratio="1:1")),
            "task_test_001",
        )
        malformed = (
            {},
            {"code": 201, "data": [{"task_id": "task"}]},
            {"code": 200, "data": {}},
            {"code": 200, "data": []},
            {"code": 200, "data": [{"task_id": ""}]},
            {"code": 200, "data": [{"task_id": "task"}, {"task_id": "other"}]},
        )
        for response in malformed:
            with self.subTest(response=response):
                with self.assertRaises(ProviderError):
                    APIMartClient(api_key="key", transport=RecordingTransport(response)).create_task(
                        GenerationRequest(prompt="ghost", ratio="1:1")
                    )

    def test_processing_twice_then_completed_polls_and_flattens_urls(self):
        processing = fixture("apimart_processing.json")
        transport = RecordingTransport(
            fixture("apimart_created.json"), processing, processing,
            fixture("apimart_completed.json"),
        )
        delays = []

        result = APIMartClient(api_key="key", transport=transport).generate(
            GenerationRequest(prompt="ghost", ratio="16:9"),
            sleeper=delays.append,
            max_polls=3,
        )

        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET", "GET", "GET"])
        self.assertEqual(delays, [1, 2])
        self.assertEqual(result.task_id, "task_test_001")
        self.assertEqual(result.image_urls, ("https://example.test/generated.png",))
        self.assertEqual(result.provider, "apimart")
        self.assertEqual(result.model, "gpt-image-2")

    def test_transient_get_error_retries_without_extra_post(self):
        transport = RecordingTransport(
            fixture("apimart_created.json"), RetryableTransportError("temporary"),
            fixture("apimart_completed.json"),
        )
        delays = []

        result = APIMartClient(api_key="key", transport=transport).generate(
            GenerationRequest(prompt="ghost", ratio="1:1"), sleeper=delays.append, max_polls=2
        )

        self.assertEqual(result.task_id, "task_test_001")
        self.assertEqual([call["method"] for call in transport.calls].count("POST"), 1)
        self.assertEqual([call["method"] for call in transport.calls].count("GET"), 2)
        self.assertEqual(delays, [1])

    def test_failed_and_cancelled_are_terminal_without_second_create(self):
        for response in (
            fixture("apimart_failed.json"),
            {"code": 200, "data": {"id": "task_test_001", "status": "cancelled"}},
        ):
            with self.subTest(status=response["data"]["status"]):
                transport = RecordingTransport(fixture("apimart_created.json"), response)
                with self.assertRaises(TaskFailedError) as raised:
                    APIMartClient(api_key="key", transport=transport).generate(
                        GenerationRequest(prompt="ghost", ratio="1:1"), sleeper=lambda _: None
                    )
                self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET"])
                if response["data"]["status"] == "failed":
                    self.assertIn("generation failed", str(raised.exception))

    def test_post_transport_failure_is_ambiguous_and_never_retried(self):
        for failure in (TimeoutError("timeout secret"), ConnectionError("lost secret")):
            with self.subTest(failure=type(failure).__name__):
                transport = RecordingTransport(failure)
                with self.assertRaises(AmbiguousSubmissionError):
                    APIMartClient(api_key="key", transport=transport).create_task(
                        GenerationRequest(prompt="ghost", ratio="1:1")
                    )
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(transport.calls[0]["method"], "POST")

    def test_post_provider_error_is_redacted_and_post_is_not_retried(self):
        secret = "secret-key-never-show"
        prompt = "this complete prompt must never show"
        reference = "data:image/png;base64,c2Vuc2l0aXZlLWJ5dGVz"
        transport = RecordingTransport(ProviderError(f"{secret} {prompt} {reference}"))

        with self.assertRaises(ProviderError) as raised:
            APIMartClient(api_key=secret, transport=transport).create_task(
                GenerationRequest(
                    prompt=prompt,
                    ratio="1:1",
                    reference_images=(reference,),
                )
            )

        message = str(raised.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn(prompt, message)
        self.assertNotIn(reference, message)
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual([call["method"] for call in transport.calls], ["POST"])

    def test_unknown_status_completed_without_urls_and_malformed_data_fail(self):
        responses = (
            {"code": 200, "data": {"id": "task_test_001", "status": "mystery"}},
            {"code": 200, "data": {"id": "task_test_001", "status": "completed", "result": {"images": []}}},
            {"code": 200, "data": []},
            {"code": 200, "data": {"id": "task_test_001"}},
        )
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaises(ProviderError):
                    APIMartClient(api_key="key", transport=RecordingTransport(response)).wait_for_task(
                        "task_test_001", sleeper=lambda _: None, max_polls=1
                    )

    def test_completed_malformed_https_url_is_reported_as_provider_error(self):
        response = {
            "code": 200,
            "data": {
                "id": "task_test_001",
                "status": "completed",
                "result": {"images": [{"url": ["https://[broken"]}]},
            },
        }

        with self.assertRaises(Exception) as raised:
            APIMartClient(api_key="key", transport=RecordingTransport(response)).wait_for_task(
                "task_test_001", sleeper=lambda _: None, max_polls=1
            )

        self.assertIsInstance(raised.exception, ProviderError)

    def test_programming_error_from_transport_is_not_retried(self):
        transport = RecordingTransport(
            ValueError("transport integration bug"), fixture("apimart_completed.json")
        )
        delays = []

        with self.assertRaises(ValueError):
            APIMartClient(api_key="key", transport=transport).wait_for_task(
                "task_test_001", sleeper=delays.append, max_polls=2
            )

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(delays, [])

    def test_poll_exhaustion_uses_capped_delays_and_names_only_task(self):
        secret = "secret-key-never-show"
        prompt = "full prompt never show"
        processing = fixture("apimart_processing.json")
        transport = RecordingTransport(processing, processing, processing, processing, processing, processing)
        delays = []

        with self.assertRaises(PollTimeoutError) as raised:
            APIMartClient(api_key=secret, transport=transport).wait_for_task(
                "task_test_001", sleeper=delays.append, max_polls=6
            )

        message = str(raised.exception)
        self.assertIn("task_test_001", message)
        self.assertNotIn(secret, message)
        self.assertNotIn(prompt, message)
        self.assertEqual(delays, [1, 2, 4, 8, 15])

    def test_nonpositive_poll_budget_fails_without_transport(self):
        for max_polls in (0, -1, True):
            with self.subTest(max_polls=max_polls):
                transport = RecordingTransport()
                with self.assertRaises(ValueError):
                    APIMartClient(api_key="key", transport=transport).wait_for_task(
                        "task_test_001", max_polls=max_polls
                    )
                self.assertEqual(transport.calls, [])

    def test_invalid_task_ids_and_base_urls_fail_before_transport(self):
        for task_id in ("", " ", "../task", "a/b", "a\\b", "a?x", "a#x"):
            with self.subTest(task_id=task_id):
                transport = RecordingTransport()
                with self.assertRaises(ValueError):
                    APIMartClient(api_key="key", transport=transport).get_task(task_id)
                self.assertEqual(transport.calls, [])
        for base_url in (
            "http://api.apimart.ai/v1",
            "https://user:pass@api.apimart.ai/v1",
            "https://api.apimart.ai/v1?token=secret",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    APIMartClient(api_key="key", base_url=base_url)

    def test_malformed_ports_are_rejected_for_base_and_reference_urls(self):
        for base_url in (
            "https://example.test:broken/v1",
            "https://example.test:65536/v1",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    APIMartClient(api_key="key", base_url=base_url)

        for reference in (
            "https://example.test:broken/reference.png",
            "https://example.test:65536/reference.png",
        ):
            with self.subTest(reference=reference):
                transport = RecordingTransport(fixture("apimart_created.json"))
                with self.assertRaises(ValueError):
                    APIMartClient(api_key="key", transport=transport).create_task(
                        GenerationRequest(
                            prompt="ghost",
                            ratio="1:1",
                            reference_images=(reference,),
                        )
                    )
                self.assertEqual(transport.calls, [])

    def test_environment_key_is_required_and_used_without_leaking_sensitive_values(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProviderError, "APIMART_API_KEY"):
                APIMartClient(transport=RecordingTransport())
        with patch.dict(os.environ, {"APIMART_API_KEY": "environment-test-key"}, clear=True):
            transport = RecordingTransport(fixture("apimart_created.json"))
            APIMartClient(transport=transport).create_task(
                GenerationRequest(prompt="ghost", ratio="1:1")
            )
            self.assertEqual(
                transport.calls[0]["headers"]["Authorization"],
                "Bearer " + "environment-test-key",
            )

        secret = "secret-key-never-show"
        prompt = "this is the complete sensitive prompt"
        reference = "data:image/png;base64,c2Vuc2l0aXZlLWJ5dGVz"
        transport = RecordingTransport(RuntimeError(f"{secret} {prompt} {reference}"))
        with self.assertRaises(AmbiguousSubmissionError) as raised:
            APIMartClient(api_key=secret, transport=transport).create_task(GenerationRequest(
                prompt=prompt, ratio="1:1", reference_images=(reference,)
            ))
        message = str(raised.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn(prompt, message)
        self.assertNotIn(reference, message)

    def test_non_200_api_code_is_redacted(self):
        secret = "secret-key-never-show"
        prompt = "this is the complete sensitive prompt"
        reference = "data:image/png;base64,c2Vuc2l0aXZlLWJ5dGVz"
        response = {"code": 500, "message": f"{secret} {prompt} {reference}"}
        with self.assertRaises(ProviderError) as raised:
            APIMartClient(api_key=secret, transport=RecordingTransport(response)).create_task(
                GenerationRequest(prompt=prompt, ratio="1:1", reference_images=(reference,))
            )
        message = str(raised.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn(prompt, message)
        self.assertNotIn(reference, message)

    def test_terminal_failure_redacts_submitted_prompt_and_reference(self):
        secret = "secret-key-never-show"
        prompt = "this full submitted prompt must never show"
        reference = "data:image/png;base64,c2Vuc2l0aXZlLWJ5dGVz"
        failed = {
            "code": 200,
            "data": {
                "id": "task_test_001",
                "status": "failed",
                "error": {"message": f"failure: {secret} {prompt} {reference}"},
            },
        }
        transport = RecordingTransport(fixture("apimart_created.json"), failed)

        with self.assertRaises(TaskFailedError) as raised:
            APIMartClient(api_key=secret, transport=transport).generate(
                GenerationRequest(
                    prompt=prompt,
                    ratio="1:1",
                    reference_images=(reference,),
                ),
                sleeper=lambda _: None,
            )

        message = str(raised.exception)
        self.assertNotIn(secret, message)
        self.assertNotIn(prompt, message)
        self.assertNotIn(reference, message)

    def test_terminal_failure_redacts_bare_data_payload_and_long_base64_token(self):
        payload = "c2Vuc2l0aXZlLWJ5dGVz"
        reference = f"data:image/png;base64,{payload}"
        unrelated_token = "QWxhZGRpbjpPcGVuU2VzYW1lMTIzNDU2Nzg5MA=="
        failed = {
            "code": 200,
            "data": {
                "id": "task_test_001",
                "status": "failed",
                "error": {"message": f"generation failed, {payload}, {unrelated_token}"},
            },
        }
        transport = RecordingTransport(fixture("apimart_created.json"), failed)

        with self.assertRaises(TaskFailedError) as raised:
            APIMartClient(api_key="key", transport=transport).generate(
                GenerationRequest(
                    prompt="ghost",
                    ratio="1:1",
                    reference_images=(reference,),
                ),
                sleeper=lambda _: None,
            )

        message = str(raised.exception)
        self.assertIn("generation failed", message)
        self.assertNotIn(payload, message)
        self.assertNotIn(unrelated_token, message)


class RequestJSONTests(unittest.TestCase):
    def test_request_json_builds_method_headers_utf8_body_and_parses_object(self):
        captured = {}

        def fake_urlopen(request, *, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeHTTPResponse(b'{"code":200,"data":{"ok":true}}')

        with patch("scripts.providers.apimart.urlopen", side_effect=fake_urlopen):
            result = request_json(
                "POST",
                "https://example.test/v1/images/generations",
                headers={
                    "Authorization": "Bearer test-key",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                payload={"prompt": "\u5c0f\u5e7d\u7075"},
                timeout=12.5,
            )

        request = captured["request"]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://example.test/v1/images/generations")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"prompt": "\u5c0f\u5e7d\u7075"})
        self.assertEqual(captured["timeout"], 12.5)
        self.assertEqual(result, {"code": 200, "data": {"ok": True}})

    def test_request_json_rejects_malformed_nonobject_and_http_status(self):
        cases = (
            FakeHTTPResponse(b"not-json"),
            FakeHTTPResponse(b"[]"),
            FakeHTTPResponse(b'{"code":200}', status=503),
        )
        for response in cases:
            with self.subTest(body=response.body, status=response.status):
                with patch("scripts.providers.apimart.urlopen", return_value=response):
                    with self.assertRaises(ProviderError):
                        request_json("GET", "https://example.test/task", headers={})

    def test_request_json_classifies_network_failure_as_retryable_provider_error(self):
        with patch("scripts.providers.apimart.urlopen", side_effect=URLError("offline")):
            with self.assertRaises(Exception) as raised:
                request_json("GET", "https://example.test/task", headers={})

        self.assertEqual(type(raised.exception).__name__, "RetryableTransportError")
        self.assertIsInstance(raised.exception, ProviderError)
        self.assertIsNone(raised.exception.__cause__)

    def test_request_json_rejects_response_larger_than_bound_during_read(self):
        response = FakeHTTPResponse(b"{" + b"x" * 64 + b"}")

        with patch("scripts.providers.apimart.MAX_JSON_RESPONSE_BYTES", 32), \
                patch("scripts.providers.apimart.urlopen", return_value=response):
            with self.assertRaisesRegex(ProviderError, "maximum"):
                request_json("GET", "https://example.test/task", headers={})

        self.assertEqual(response.read_sizes, [33])


if __name__ == "__main__":
    unittest.main()
