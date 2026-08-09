"""Environment-keyed client for OpenAI Images-compatible endpoints."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import os
import re
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

from scripts.common import MAX_DOWNLOAD_BYTES
from scripts.providers.apimart import (
    AmbiguousSubmissionError,
    GenerationRequest,
    ProviderError,
    RetryableTransportError,
    request_json,
)


DEFAULT_QUALITY_MAP = {
    "draft": "low",
    "standard": "medium",
    "high": "high",
}

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_LONG_BASE64_RE = re.compile(
    r"(?<![A-Za-z0-9+/])(?:[A-Za-z0-9+/]{32,}={0,2})(?![A-Za-z0-9+/=])"
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_GENERATIONS_PATH = "/images/generations"


@dataclass(frozen=True)
class ImagePayload:
    url: str | None = None
    data: bytes | None = None


def _split_safe_url(value: object):
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        parsed.port
        parsed.username
        parsed.password
    except (TypeError, ValueError):
        return None
    if not parsed.netloc or not parsed.hostname:
        return None
    return parsed


def validate_base_url(base_url: str, *, allow_local_http: bool = False) -> str:
    """Validate and normalize an Images-compatible API base URL."""

    if isinstance(base_url, str) and ("?" in base_url or "#" in base_url):
        raise ValueError("base_url must not contain a query or fragment")
    parsed = _split_safe_url(base_url)
    if parsed is None:
        raise ValueError("base_url must be a valid HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query or fragment")

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    if scheme == "https":
        pass
    elif scheme == "http" and allow_local_http and hostname in _LOOPBACK_HOSTS:
        pass
    else:
        raise ValueError("base_url must use HTTPS or approved loopback HTTP")
    return base_url.rstrip("/")


def _valid_result_url(value: object, *, allow_local_http: bool) -> bool:
    parsed = _split_safe_url(value)
    if parsed is None or parsed.username is not None or parsed.password is not None:
        return False
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return True
    return (
        scheme == "http"
        and allow_local_http
        and parsed.hostname.lower() in _LOOPBACK_HOSTS
    )


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key_env: str = "CUSTOM_IMAGE_API_KEY",
        allow_local_http: bool = False,
        transport: Callable[..., dict[str, object]] = request_json,
        quality_map: Mapping[str, str] | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(api_key_env, str) or not _ENV_NAME_RE.fullmatch(api_key_env):
            raise ValueError("api_key_env must be a valid environment variable name")

        normalized_base = validate_base_url(
            base_url,
            allow_local_http=allow_local_http,
        )
        if normalized_base.endswith(_GENERATIONS_PATH):
            self._endpoint = normalized_base
        else:
            self._endpoint = f"{normalized_base}{_GENERATIONS_PATH}"
        self._model = model
        self._api_key_env = api_key_env
        self._allow_local_http = allow_local_http
        self._transport = transport
        if quality_map is None:
            self._quality_map: dict[str, str] | None = None
        else:
            if not isinstance(quality_map, Mapping) or not quality_map:
                raise ValueError("quality_map must be a non-empty mapping")
            normalized_quality_map: dict[str, str] = {}
            for quality, provider_value in quality_map.items():
                if quality not in DEFAULT_QUALITY_MAP:
                    raise ValueError("quality_map contains an unsupported quality")
                if not isinstance(provider_value, str) or not provider_value.strip():
                    raise ValueError("quality_map values must be non-empty strings")
                normalized_quality_map[quality] = provider_value.strip()
            self._quality_map = normalized_quality_map

    def _validate_request(self, request: GenerationRequest) -> None:
        if not isinstance(request, GenerationRequest):
            raise ValueError("request must be a GenerationRequest")
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if not isinstance(request.ratio, str) or not request.ratio.strip():
            raise ValueError("ratio must be a non-empty string")
        if request.quality not in DEFAULT_QUALITY_MAP:
            raise ValueError("quality must be draft, standard, or high")
        if self._quality_map is not None and request.quality not in self._quality_map:
            raise ValueError("quality is not supported by this provider")
        if type(request.count) is not int or request.count <= 0:
            raise ValueError("count must be a positive integer")
        if request.reference_images:
            raise ProviderError(
                "reference_images are not supported by this provider in phase 1"
            )

    @staticmethod
    def _redact(message: str, *, api_key: str, prompt: str) -> str:
        redacted = message
        for sensitive in (api_key, prompt):
            if sensitive:
                redacted = redacted.replace(sensitive, "[REDACTED]")
        return _LONG_BASE64_RE.sub("[REDACTED]", redacted)

    def _parse_response(self, response: object) -> tuple[ImagePayload, ...]:
        if not isinstance(response, dict):
            raise ProviderError("image provider returned a non-object response")
        data = response.get("data")
        if not isinstance(data, list) or not data:
            raise ProviderError("image provider returned malformed or empty data")

        payloads: list[ImagePayload] = []
        for item in data:
            if not isinstance(item, dict):
                raise ProviderError("image provider returned a non-object result item")
            url = item.get("url")
            encoded = item.get("b64_json")
            has_url = isinstance(url, str) and bool(url)
            has_encoded = isinstance(encoded, str) and bool(encoded)
            if has_url == has_encoded:
                raise ProviderError("image result must contain exactly one image payload")
            if "url" in item and not has_url:
                raise ProviderError("image result contains an invalid URL value")
            if "b64_json" in item and not has_encoded:
                raise ProviderError("image result contains an invalid Base64 value")

            if has_url:
                if not _valid_result_url(
                    url,
                    allow_local_http=self._allow_local_http,
                ):
                    raise ProviderError("image result contains an unsafe URL")
                payloads.append(ImagePayload(url=url))
                continue

            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise ProviderError("image result contains invalid Base64 data") from None
            if not decoded:
                raise ProviderError("image result decoded to empty bytes")
            if len(decoded) > MAX_DOWNLOAD_BYTES:
                raise ProviderError("image result exceeds the maximum allowed size")
            payloads.append(ImagePayload(data=decoded))
        return tuple(payloads)

    def generate(self, request: GenerationRequest) -> Sequence[ImagePayload]:
        api_key = os.environ.get(self._api_key_env)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderError(f"{self._api_key_env} is required and must not be blank")
        self._validate_request(request)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "model": self._model,
            "prompt": request.prompt,
            "n": request.count,
            "size": request.ratio,
        }
        if self._quality_map is not None:
            payload["quality"] = self._quality_map[request.quality]
        try:
            response = self._transport(
                "POST",
                self._endpoint,
                headers=headers,
                payload=payload,
            )
        except RetryableTransportError:
            raise AmbiguousSubmissionError(
                "image submission outcome is ambiguous; the POST was not retried"
            ) from None
        except ProviderError as exc:
            message = self._redact(
                str(exc),
                api_key=api_key,
                prompt=request.prompt,
            )
            raise ProviderError(message or "image provider request failed") from None
        except Exception as exc:
            message = self._redact(
                str(exc),
                api_key=api_key,
                prompt=request.prompt,
            )
            raise AmbiguousSubmissionError(
                "image submission outcome is ambiguous; the POST was not retried"
                + (f": {message}" if message else "")
            ) from None
        return self._parse_response(response)
