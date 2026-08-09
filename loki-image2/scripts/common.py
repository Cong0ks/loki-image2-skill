"""Security-conscious shared primitives for the Loki Image2 skill."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import ipaddress
import json
from pathlib import Path
import re
import shutil
import stat
import tempfile
import unicodedata
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from uuid import uuid4


MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_DOWNLOAD_REDIRECTS = 5
ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

_REDACTED = "[REDACTED]"
_METADATA_KEYS = (
    "brand", "style", "ratio", "quality", "provider", "model",
    "created_at", "task_id", "correlation_id", "output_files",
)
_SENSITIVE_QUERY_KEYS = (
    r"token|access_token|id_token|key|api_key|apikey|signature|sig|policy|"
    r"key-pair-id|awsaccesskeyid|googleaccessid|x-amz-[^=&\s]+|"
    r"x-goog-[^=&\s]+|sv|se|sp|sr|spr|sip|st|skoid|sktid|skt|ske|sks|skv"
)
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def safe_slug(value: str) -> str:
    """Return a Unicode-safe, path-free slug, or reject unsafe input."""
    if not isinstance(value, str):
        raise ValueError("Slug value must be text")
    stripped = value.strip()
    if (
        not stripped
        or "/" in stripped
        or "\\" in stripped
        or re.match(r"^[A-Za-z]:", stripped)
        or stripped == ".."
    ):
        raise ValueError("Slug contains an unsafe path")

    decomposed = unicodedata.normalize("NFKD", stripped).casefold()
    normalized = "".join(
        character
        for character in decomposed
        if unicodedata.category(character) != "Mn"
    )
    words = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    if not words:
        raise ValueError("Slug is empty after normalization")
    return "-".join(words)


def ensure_within(root: Path, candidate: Path) -> Path:
    """Resolve *candidate* and prove that it is inside (or is) *root*."""
    resolved_root = Path(root).resolve()
    resolved_candidate = Path(candidate).resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Path escapes the permitted root") from exc
    return resolved_candidate


def _is_reparse_point(path: Path) -> bool:
    """Return whether an existing path is a symlink or Windows reparse point."""
    try:
        details = Path(path).lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("Unable to validate output path") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & reparse_flag
    )


def _trusted_path(root: Path, candidate: Path) -> Path:
    """Validate lexical and resolved containment without traversing reparse points."""
    trusted_root = Path(root).resolve()
    candidate_path = Path(candidate)
    try:
        relative = candidate_path.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError("Path escapes the permitted root") from exc

    current = trusted_root
    for component in relative.parts:
        current /= component
        if _is_reparse_point(current):
            raise ValueError("Output path contains a link or reparse point")
    return ensure_within(trusted_root, candidate_path)


def trusted_existing_directory(root: Path, directory: Path) -> Path:
    """Return an existing directory proven inside *root* without reparse traversal."""
    trusted_root = Path(root).resolve()
    candidate = Path(directory)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    trusted_directory = _trusted_path(trusted_root, candidate)
    if not trusted_directory.is_dir():
        raise ValueError("Trusted directory does not exist or is not a directory")
    return trusted_directory


def _prepare_trusted_directory(root: Path, directory: Path) -> Path:
    """Create one directory level and validate it before and after creation."""
    directory = Path(directory)
    _trusted_path(root, directory)
    try:
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise ValueError("Unable to create a trusted output directory") from exc
    trusted_directory = _trusted_path(root, directory)
    if not trusted_directory.is_dir():
        raise ValueError("Output path is not a directory")
    return trusted_directory


def _create_unique_trusted_directory(root: Path, directory: Path) -> Path:
    """Atomically create *directory*, adding an unguessable suffix on collision."""
    base = Path(directory)
    for attempt in range(16):
        candidate = base if attempt == 0 else base.with_name(f"{base.name}-{uuid4().hex[:12]}")
        _trusted_path(root, candidate)
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            raise ValueError("Unable to create a unique task output directory") from exc
        trusted_directory = _trusted_path(root, candidate)
        if not trusted_directory.is_dir():
            raise ValueError("Task output path is not a directory")
        return trusted_directory
    raise ValueError("Unable to allocate a unique task output directory")


def _trusted_file_target(root: Path, target: Path) -> Path:
    """Validate a final file target and reject non-file existing targets."""
    trusted_target = _trusted_path(root, target)
    if trusted_target.exists() and not trusted_target.is_file():
        raise ValueError("Output target is not a regular file")
    return trusted_target


def _atomic_write_text(root: Path, target: Path, value: str) -> None:
    """Write text through a temporary sibling, replacing only a validated target."""
    target = _trusted_file_target(root, target)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}-",
            suffix=".part",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
        _trusted_path(root, temporary)
        target = _trusted_file_target(root, target)
        temporary.replace(target)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_copy_file(root: Path, source: Path, target: Path) -> None:
    """Copy a file through a temporary sibling, then replace a validated target."""
    target = _trusted_file_target(root, target)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=target.parent,
            prefix=f".{target.name}-",
            suffix=".part",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        shutil.copyfile(source, temporary)
        _trusted_path(root, temporary)
        target = _trusted_file_target(root, target)
        temporary.replace(target)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def redact_text(value: str) -> str:
    """Redact common credentials while leaving non-sensitive context intact."""
    if not isinstance(value, str):
        return value

    redacted = re.sub(
        r"(?im)(authorization\s*:\s*)[^\r\n]*",
        rf"\1{_REDACTED}",
        value,
    )
    redacted = re.sub(r"(?i)\bbearer\s+[^\s,;]+", f"Bearer {_REDACTED}", redacted)
    redacted = re.sub(r"(?i)\bsk-[a-z0-9][a-z0-9._-]*", _REDACTED, redacted)
    return re.sub(
        rf"(?i)((?:[?&]|\b)(?:{_SENSITIVE_QUERY_KEYS})=)[^&#\s]*",
        rf"\1{_REDACTED}",
        redacted,
    )


def cleanup_old_logs(
    log_dir: Path,
    now: datetime | None = None,
    retention_days: int = 7,
) -> int:
    """Delete regular ``.log`` files older than the strict retention cutoff."""
    directory = Path(log_dir)
    if _is_reparse_point(directory):
        raise ValueError("Log directory is a link or reparse point")
    if not directory.exists():
        return 0
    directory = trusted_existing_directory(directory.parent.resolve(), directory)
    reference = now or datetime.now()
    cutoff = reference - timedelta(days=retention_days)
    deleted = 0
    for entry in directory.iterdir():
        if _is_reparse_point(entry) or not entry.is_file() or entry.suffix != ".log":
            continue
        modified = datetime.fromtimestamp(entry.stat().st_mtime, tz=reference.tzinfo)
        if modified < cutoff:
            entry.unlink()
            deleted += 1
    return deleted


def write_error_log(
    log_dir: Path,
    *,
    error_type: str,
    provider: str,
    summary: str,
    http_status: int | None = None,
    task_id: str | None = None,
    correlation_id: str | None = None,
    now: datetime | None = None,
    trusted_root: Path | None = None,
) -> Path:
    """Write a redacted structured error log after applying retention cleanup."""
    directory = Path(log_dir).expanduser()
    if not directory.is_absolute():
        directory = Path.cwd() / directory
    root = Path(trusted_root).expanduser().resolve() if trusted_root is not None else directory.parent.resolve()
    _trusted_path(root, directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory = trusted_existing_directory(root, directory)
    timestamp = now or datetime.now()
    cleanup_old_logs(directory, now=timestamp, retention_days=7)
    payload = {
        "timestamp": redact_text(timestamp.isoformat()),
        "error_type": redact_text(error_type),
        "provider": redact_text(provider),
        "http_status": http_status,
        "task_id": redact_text(task_id) if task_id is not None else None,
        "correlation_id": redact_text(correlation_id) if correlation_id is not None else None,
        "summary": redact_text(summary),
    }
    path = directory / f"error-{timestamp.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex}.log"
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _safe_metadata_value(value: object) -> object:
    """Keep metadata JSON-serializable without allowing accidental secret text."""
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_safe_metadata_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_metadata_value(item) for key, item in value.items()}
    return redact_text(str(value))


def _validate_download_url(url: str, *, allow_local_http: bool) -> None:
    """Validate a requested or final download URL without resolving DNS."""
    try:
        parsed = urlsplit(url)
        parsed.port
    except (TypeError, ValueError):
        raise ValueError("Image URL is malformed") from None
    if (
        not isinstance(url, str)
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(character.isspace() for character in url)
    ):
        raise ValueError("Image URL contains unsafe components")

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    if scheme == "http":
        if not allow_local_http or hostname not in _LOOPBACK_HOSTS:
            raise ValueError("Only HTTPS or approved loopback HTTP image URLs are supported")
        if any(
            re.fullmatch(_SENSITIVE_QUERY_KEYS, key, flags=re.IGNORECASE)
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        ):
            raise ValueError("Loopback HTTP image URL must not carry credentials or signatures")
        return
    if scheme != "https":
        raise ValueError("Only HTTPS or approved loopback HTTP image URLs are supported")
    if hostname in _LOOPBACK_HOSTS:
        raise ValueError("HTTPS image URL must not target a local host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("HTTPS image URL must not target a non-public IP address")


def _signature_extension(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    return None


class _ValidatingNoRedirectHandler(HTTPRedirectHandler):
    """Validate redirect targets at urllib's boundary, then leave following to us."""

    def __init__(self, *, allow_local_http: bool):
        super().__init__()
        self.allow_local_http = allow_local_http

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        target = urljoin(request.full_url, new_url)
        _validate_download_url(target, allow_local_http=self.allow_local_http)
        return None


def _response_status(response: object) -> int | None:
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    getcode = getattr(response, "getcode", None)
    status = getcode() if callable(getcode) else None
    return status if isinstance(status, int) else None


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _open_validated_download_response(
    url: str,
    *,
    opener: Callable,
    timeout: float,
    allow_local_http: bool,
) -> object:
    """Follow a bounded redirect chain, validating every target before opening it."""
    current_url = url
    visited = {url}
    redirects = 0
    while True:
        try:
            response = opener(current_url, timeout=timeout)
        except HTTPError as exception:
            if exception.code not in _REDIRECT_STATUSES:
                raise
            response = exception

        if _response_status(response) not in _REDIRECT_STATUSES:
            geturl = getattr(response, "geturl", None)
            final_url = geturl() if callable(geturl) else None
            _validate_download_url(
                final_url if isinstance(final_url, str) and final_url else current_url,
                allow_local_http=allow_local_http,
            )
            return response

        try:
            location = response.headers.get("Location")
            if not isinstance(location, str) or not location.strip():
                raise ValueError("Image redirect is missing a valid Location")
            next_url = urljoin(current_url, location)
            _validate_download_url(next_url, allow_local_http=allow_local_http)
            if next_url in visited:
                raise ValueError("Image redirect loop detected")
            if redirects >= MAX_DOWNLOAD_REDIRECTS:
                raise ValueError("Image redirect limit exceeded")
            visited.add(next_url)
            redirects += 1
            current_url = next_url
        finally:
            _close_response(response)


def save_task_artifacts(
    project_dir: Path,
    *,
    topic: str,
    prompt: str,
    metadata: dict[str, object],
    images: list[Path],
    now: datetime | None = None,
) -> Path:
    """Save final task files under the timestamped, safe output directory."""
    timestamp = now or datetime.now()
    project_root = Path(project_dir).resolve()
    project_root.mkdir(parents=True, exist_ok=True)
    if not project_root.is_dir():
        raise ValueError("Project path is not a directory")
    output_root = _prepare_trusted_directory(project_root, project_root / "output")
    base_dir = _prepare_trusted_directory(project_root, output_root / "loki-image2")
    output_dir = _create_unique_trusted_directory(
        project_root,
        base_dir / f"{timestamp.strftime('%Y%m%d-%H%M%S')}-{safe_slug(topic)}",
    )
    prompt_path = _trusted_file_target(output_dir, output_dir / "prompt.md")
    metadata_path = _trusted_file_target(output_dir, output_dir / "metadata.json")

    copied_files: list[str] = []
    used_names: set[str] = set()
    for image in images:
        source = Path(image)
        filename = source.name
        stem, suffix = source.stem, source.suffix
        counter = 2
        destination = _trusted_file_target(output_dir, output_dir / filename)
        while filename in used_names or destination.exists():
            filename = f"{stem}-{counter}{suffix}"
            counter += 1
            destination = _trusted_file_target(output_dir, output_dir / filename)
        _atomic_copy_file(output_dir, source, destination)
        used_names.add(filename)
        copied_files.append(filename)

    output_metadata = {
        key: _safe_metadata_value(metadata[key])
        for key in _METADATA_KEYS
        if key in metadata and key not in {"created_at", "output_files"}
    }
    output_metadata["created_at"] = timestamp.isoformat()
    output_metadata["output_files"] = copied_files
    _atomic_write_text(output_dir, prompt_path, prompt)
    _atomic_write_text(
        output_dir,
        metadata_path,
        json.dumps(output_metadata, ensure_ascii=False, indent=2) + "\n",
    )
    return output_dir


def download_image(
    url: str,
    destination_stem: Path,
    *,
    opener: Callable | None = None,
    timeout: float = 30.0,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    allow_local_http: bool = False,
) -> Path:
    """Download a validated remote image atomically to a MIME-derived suffix."""
    _validate_download_url(url, allow_local_http=allow_local_http)
    if max_bytes < 1:
        raise ValueError("Maximum download size must be positive")

    destination_stem = Path(destination_stem)
    destination_stem.parent.mkdir(parents=True, exist_ok=True)
    if opener is None:
        opener = build_opener(
            _ValidatingNoRedirectHandler(allow_local_http=allow_local_http)
        ).open
    response = None
    temporary: Path | None = None
    try:
        response = _open_validated_download_response(
            url,
            opener=opener,
            timeout=timeout,
            allow_local_http=allow_local_http,
        )
        raw_content_type = response.headers.get("Content-Type", "")
        content_type = str(raw_content_type).split(";", 1)[0].strip().lower()
        extension = ALLOWED_IMAGE_TYPES.get(content_type)
        if extension is None:
            raise ValueError("Remote response is not an allowed image type")

        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination_stem.parent,
            prefix=f".{destination_stem.name}-",
            suffix=".part",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            total = 0
            while chunk := response.read(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Image download exceeds the maximum allowed size")
                handle.write(chunk)
        if total == 0:
            raise ValueError("Image download body is empty")
        with temporary.open("rb") as handle:
            detected_extension = _signature_extension(handle.read(12))
        if detected_extension != extension:
            raise ValueError("Image response MIME type does not match its file signature")

        final_path = destination_stem.with_suffix(extension)
        temporary.rename(final_path)
        temporary = None
        return final_path
    finally:
        if response is not None:
            _close_response(response)
        if temporary is not None and temporary.exists():
            temporary.unlink()
