"""Standard-library APIMart gpt-image-2 asynchronous provider client."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import os
import re
import time
from typing import Callable, Literal, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


QUALITY_TO_RESOLUTION = {
    "draft": "1k",
    "standard": "2k",
    "high": "4k",
}
MAX_JSON_RESPONSE_BYTES = 2 * 1024 * 1024

_WAITING_STATUSES = frozenset({"pending", "submitted", "processing"})
_FAILED_STATUSES = frozenset({"failed", "cancelled"})
_POLL_DELAYS = (1, 2, 4, 8, 15)
_TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]*\Z")
_DATA_IMAGE_RE = re.compile(
    r"data:image/(?:png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})\Z"
)
_LONG_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{32,}={0,2})(?![A-Za-z0-9+/=])"
)


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    ratio: str
    quality: Literal["draft", "standard", "high"] = "standard"
    count: int = 1
    reference_images: Sequence[str] = ()


@dataclass(frozen=True)
class GenerationResult:
    task_id: str
    image_urls: Sequence[str]
    provider: str = "apimart"
    model: str = "gpt-image-2"


class ProviderError(RuntimeError):
    """The provider rejected or returned an invalid response."""


class AmbiguousSubmissionError(ProviderError):
    """A POST may have reached APIMart, so it must not be retried."""


class TaskFailedError(ProviderError):
    """APIMart reported a terminal task failure."""


class PollTimeoutError(ProviderError):
    """The task did not finish within the caller's polling budget."""


class RetryableTransportError(ProviderError):
    """A retry-safe failure occurred before a GET response was received."""


def _response_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProviderError("APIMart returned a non-object JSON response")
    code = value.get("code")
    if type(code) is not int or code != 200:
        raise ProviderError("APIMart returned an unsuccessful API code")
    return value


def request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Mapping[str, object] | None = None,
    timeout: float = 30.0,
) -> dict[str, object]:
    """Issue one HTTP request and return a top-level JSON object."""

    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, headers=dict(headers), method=method.upper())

    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            raw_body = response.read(MAX_JSON_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise ProviderError(f"HTTP request failed with status {exc.code}") from None
    except (URLError, TimeoutError, ConnectionError, OSError):
        raise RetryableTransportError("HTTP transport failed") from None

    if type(status) is not int or not 200 <= status < 300:
        suffix = f" with status {status}" if isinstance(status, int) else ""
        raise ProviderError(f"HTTP request failed{suffix}")
    if not isinstance(raw_body, bytes):
        raise ProviderError("HTTP endpoint returned a non-bytes response body")
    if len(raw_body) > MAX_JSON_RESPONSE_BYTES:
        raise ProviderError("HTTP JSON response exceeds the maximum allowed size")
    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderError("HTTP endpoint returned malformed JSON") from None
    if not isinstance(decoded, dict):
        raise ProviderError("HTTP endpoint returned a non-object JSON response")
    return decoded


class APIMartClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = "https://api.apimart.ai/v1",
        transport: Callable[..., dict[str, object]] = request_json,
    ) -> None:
        resolved_key = os.environ.get("APIMART_API_KEY") if api_key is None else api_key
        if not isinstance(resolved_key, str) or not resolved_key.strip():
            raise ProviderError("APIMART_API_KEY is required and must not be blank")

        self._base_url = self._validate_base_url(base_url)
        self._transport = transport
        self._api_key = resolved_key
        self._task_sensitive: dict[str, tuple[str, ...]] = {}
        self._headers = {
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty HTTPS URL")
        try:
            parsed = urlsplit(base_url)
            parsed.port
        except ValueError:
            raise ValueError("base_url must be a valid HTTPS URL") from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must use HTTPS and must not contain credentials, query, or fragment"
            )
        return base_url.rstrip("/")

    @staticmethod
    def _validate_request(request: GenerationRequest) -> None:
        if not isinstance(request, GenerationRequest):
            raise ValueError("request must be a GenerationRequest")
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if not isinstance(request.ratio, str) or not request.ratio.strip():
            raise ValueError("ratio must be a non-empty string")
        if request.quality not in QUALITY_TO_RESOLUTION:
            raise ValueError("quality must be draft, standard, or high")
        if type(request.count) is not int or request.count <= 0:
            raise ValueError("count must be a positive integer")
        if isinstance(request.reference_images, (str, bytes)):
            raise ValueError("reference_images must be a sequence of image references")
        try:
            references = tuple(request.reference_images)
        except TypeError:
            raise ValueError("reference_images must be a sequence") from None
        if len(references) > 16:
            raise ValueError("reference_images accepts at most 16 values")
        for reference in references:
            if not APIMartClient._valid_reference(reference):
                raise ValueError("reference image must be a safe HTTPS URL or supported data URI")

    @staticmethod
    def _valid_reference(value: object) -> bool:
        if not isinstance(value, str) or not value or value != value.strip():
            return False
        data_match = _DATA_IMAGE_RE.fullmatch(value)
        if data_match:
            try:
                base64.b64decode(data_match.group(1), validate=True)
            except (binascii.Error, ValueError):
                return False
            return True
        return APIMartClient._valid_https_url(value)

    @staticmethod
    def _valid_https_url(value: object) -> bool:
        if not isinstance(value, str) or not value or value != value.strip():
            return False
        if any(character.isspace() for character in value):
            return False
        try:
            parsed = urlsplit(value)
            parsed.port
            return (
                parsed.scheme == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            return False

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if (
            not isinstance(task_id, str)
            or not _TASK_ID_RE.fullmatch(task_id)
            or ".." in task_id
        ):
            raise ValueError("task_id must be a non-empty simple identifier")

    @staticmethod
    def _validated_data(response: object) -> dict[str, object]:
        envelope = _response_object(response)
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise ProviderError("APIMart task response has malformed data")
        task_id = data.get("id")
        status = data.get("status")
        if not isinstance(task_id, str) or not task_id or not isinstance(status, str) or not status:
            raise ProviderError("APIMart task response is missing id or status")
        return data

    def _redact(self, message: str, extra_values: Sequence[str] = ()) -> str:
        redacted = message
        sensitive_values = [self._api_key, *extra_values]
        for value in extra_values:
            data_match = _DATA_IMAGE_RE.fullmatch(value)
            if data_match:
                sensitive_values.append(data_match.group(1))
        for sensitive in sensitive_values:
            if sensitive:
                redacted = redacted.replace(sensitive, "[REDACTED]")
        return _LONG_BASE64_RE.sub("[REDACTED]", redacted)

    def create_task(self, request: GenerationRequest) -> str:
        self._validate_request(request)
        payload: dict[str, object] = {
            "model": "gpt-image-2",
            "prompt": request.prompt,
            "n": request.count,
            "size": request.ratio,
            "resolution": QUALITY_TO_RESOLUTION[request.quality],
        }
        references = list(request.reference_images)
        if references:
            payload["image_urls"] = references

        try:
            response = self._transport(
                "POST",
                f"{self._base_url}/images/generations",
                headers=self._headers,
                payload=payload,
            )
        except RetryableTransportError:
            raise AmbiguousSubmissionError(
                "APIMart submission outcome is ambiguous; the POST was not retried"
            ) from None
        except ProviderError as exc:
            safe_message = self._redact(
                str(exc), (request.prompt, *references)
            )
            raise ProviderError(safe_message or "APIMart submission failed") from None
        except Exception:
            raise AmbiguousSubmissionError(
                "APIMart submission outcome is ambiguous; the POST was not retried"
            ) from None

        envelope = _response_object(response)
        data = envelope.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise ProviderError("APIMart creation response has malformed data")
        task_id = data[0].get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ProviderError("APIMart creation response is missing task_id")
        self._task_sensitive[task_id] = (request.prompt, *references)
        return task_id

    def get_task(self, task_id: str) -> dict[str, object]:
        self._validate_task_id(task_id)
        try:
            response = self._transport(
                "GET",
                f"{self._base_url}/tasks/{quote(task_id, safe='')}",
                headers=self._headers,
            )
        except RetryableTransportError:
            raise
        except ProviderError as exc:
            safe_message = self._redact(
                str(exc), self._task_sensitive.get(task_id, ())
            )
            raise ProviderError(safe_message or "APIMart task request failed") from None
        data = self._validated_data(response)
        if data["id"] != task_id:
            raise ProviderError("APIMart task response id does not match the requested task")
        return data

    def wait_for_task(
        self,
        task_id: str,
        *,
        sleeper: Callable[[float], object] = time.sleep,
        max_polls: int = 60,
    ) -> GenerationResult:
        self._validate_task_id(task_id)
        if type(max_polls) is not int or max_polls <= 0:
            raise ValueError("max_polls must be a positive integer")

        for poll_index in range(max_polls):
            try:
                task = self.get_task(task_id)
            except RetryableTransportError:
                if poll_index + 1 < max_polls:
                    sleeper(_POLL_DELAYS[min(poll_index, len(_POLL_DELAYS) - 1)])
                continue

            status = task["status"]
            if status in _WAITING_STATUSES:
                if poll_index + 1 < max_polls:
                    sleeper(_POLL_DELAYS[min(poll_index, len(_POLL_DELAYS) - 1)])
                continue
            if status == "completed":
                urls = self._completed_urls(task)
                self._task_sensitive.pop(task_id, None)
                return GenerationResult(task_id=task_id, image_urls=urls)
            if status in _FAILED_STATUSES:
                message = f"APIMart task {task_id} {status}"
                error = task.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    safe_detail = self._redact(
                        error["message"], self._task_sensitive.get(task_id, ())
                    )
                    if safe_detail.strip():
                        message = f"{message}: {safe_detail}"
                self._task_sensitive.pop(task_id, None)
                raise TaskFailedError(message)
            raise ProviderError("APIMart task response contains an unknown status")

        raise PollTimeoutError(f"APIMart task {task_id} exhausted its polling budget")

    def _completed_urls(self, task: Mapping[str, object]) -> tuple[str, ...]:
        result = task.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("images"), list):
            raise ProviderError("APIMart completed task has malformed result images")
        urls: list[str] = []
        for image in result["images"]:
            if not isinstance(image, dict) or not isinstance(image.get("url"), list):
                continue
            for candidate in image["url"]:
                if self._valid_https_url(candidate):
                    urls.append(candidate)
        if not urls:
            raise ProviderError("APIMart completed task contains no valid HTTPS image URLs")
        return tuple(urls)

    def generate(
        self,
        request: GenerationRequest,
        *,
        sleeper: Callable[[float], object] = time.sleep,
        max_polls: int = 60,
    ) -> GenerationResult:
        task_id = self.create_task(request)
        return self.wait_for_task(task_id, sleeper=sleeper, max_polls=max_polls)
