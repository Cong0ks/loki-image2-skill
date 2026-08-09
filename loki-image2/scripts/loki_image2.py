"""Deterministic, JSON-only command line entry point for Loki Image2."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Sequence
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from uuid import uuid4


SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

from scripts.brand_store import Brand, create_user_brand, list_brands, load_brand
from scripts.common import (
    MAX_DOWNLOAD_BYTES,
    cleanup_old_logs,
    download_image,
    safe_slug,
    save_task_artifacts,
    trusted_existing_directory,
    write_error_log,
)
from scripts.providers.apimart import (
    APIMartClient,
    AmbiguousSubmissionError,
    GenerationRequest,
    QUALITY_TO_RESOLUTION,
)
from scripts.providers.openai_compatible import (
    DEFAULT_QUALITY_MAP,
    ImagePayload,
    OpenAICompatibleClient,
    validate_base_url,
)


MAX_PROMPT_BYTES = 1024 * 1024
MAX_REFERENCE_IMAGE_BYTES = MAX_DOWNLOAD_BYTES
BUILTIN_BRANDS = SKILL_ROOT / "assets" / "brands"
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_QUALITY_VALUES = ("draft", "standard", "high")


class CLIError(ValueError):
    """A safe, expected command-line failure."""


class CLIUsageError(CLIError):
    """An argparse-level failure whose raw argv must not be echoed."""


class JSONArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("add_help", False)
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):
        raise CLIUsageError("Invalid command arguments")


def _runtime_root() -> Path:
    configured = os.environ.get("LOKI_IMAGE_HOME")
    candidate = Path(configured) if configured else Path.home() / ".codex" / "loki-image"
    return candidate.expanduser().resolve()


def _brand_roots() -> tuple[Path, Path]:
    return BUILTIN_BRANDS, _runtime_root() / "brands"


def _cleanup_existing_error_logs() -> None:
    runtime_root = _runtime_root()
    try:
        log_dir = trusted_existing_directory(runtime_root, runtime_root / "logs")
        cleanup_old_logs(log_dir)
    except Exception:
        # Startup retention is best-effort and must never block a safe command.
        return


def _brand_payload(brand: Brand, *, include_asset: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": brand.id,
        "display_name": brand.display_name,
        "source": brand.source,
        "anchors": list(brand.anchors),
        "palette": list(brand.default_palette),
    }
    if include_asset:
        payload["character_image"] = str(brand.character_image)
    return payload


def _read_prompt(path_value: str) -> str:
    path = Path(path_value)
    try:
        size = path.stat().st_size
    except OSError:
        raise CLIError("Prompt file is unavailable") from None
    if not path.is_file():
        raise CLIError("Prompt file is unavailable")
    if size > MAX_PROMPT_BYTES:
        raise CLIError("Prompt file exceeds the 1 MiB limit")
    try:
        prompt = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise CLIError("Prompt file must be readable UTF-8 text") from None
    if not prompt.strip():
        raise CLIError("Prompt must not be empty")
    return prompt


def _reference_image_data_uri(path_value: str) -> str:
    path = Path(path_value).expanduser()
    descriptor: int | None = None
    try:
        descriptor = _open_reference_image(path)
        opened_before = os.fstat(descriptor)
        path_before = path.lstat()
        _validate_reference_image_identity(opened_before, path_before)
        if opened_before.st_size < 1 or opened_before.st_size > MAX_REFERENCE_IMAGE_BYTES:
            raise CLIError("Reference image file size is invalid")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            data = handle.read(MAX_REFERENCE_IMAGE_BYTES + 1)
            opened_after = os.fstat(handle.fileno())
        path_after = path.lstat()
        _validate_reference_image_identity(opened_after, path_after)
        if (
            not os.path.samestat(opened_before, opened_after)
            or opened_before.st_size != opened_after.st_size
            or len(data) != opened_after.st_size
        ):
            raise CLIError("Reference image file changed while it was being read")
    except CLIError:
        raise
    except OSError:
        raise CLIError("Reference image file is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not data or len(data) > MAX_REFERENCE_IMAGE_BYTES:
        raise CLIError("Reference image file size is invalid")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        media_type = "image/jpeg"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        media_type = "image/webp"
    else:
        raise CLIError("Reference image file has an unsupported signature")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def _open_reference_image(path: Path) -> int:
    """Open a reference image without following its final path component."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            flags |= nofollow
        return os.open(path, flags)
    return _open_windows_reference_image(path, flags)


def _open_windows_reference_image(path: Path, flags: int) -> int:
    """Open a Windows leaf using OPEN_REPARSE_POINT, then transfer its handle to an fd."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    file_read_data = 0x0001
    file_share_all = 0x0001 | 0x0002 | 0x0004
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_attribute_reparse_point = 0x0400
    file_attribute_tag_info_class = 9

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        file_read_data,
        file_share_all,
        None,
        open_existing,
        file_flag_open_reparse_point,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        error_code = ctypes.get_last_error()
        raise OSError(error_code, ctypes.FormatError(error_code), str(path))

    transferred = False
    try:
        attributes = FileAttributeTagInfo()
        if not get_information(
            handle,
            file_attribute_tag_info_class,
            ctypes.byref(attributes),
            ctypes.sizeof(attributes),
        ):
            error_code = ctypes.get_last_error()
            raise OSError(error_code, ctypes.FormatError(error_code), str(path))
        if attributes.FileAttributes & file_attribute_reparse_point:
            raise CLIError("Reference image file must not be a link or reparse point")
        descriptor = msvcrt.open_osfhandle(handle, flags)
        transferred = True
        return descriptor
    finally:
        if not transferred:
            close_handle(handle)


def _validate_reference_image_identity(opened: os.stat_result, path_details: os.stat_result) -> None:
    """Prove that path and fd still name the same stable regular file."""
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(path_details.st_mode) or bool(
        getattr(path_details, "st_file_attributes", 0) & reparse_flag
    ):
        raise CLIError("Reference image file must not be a link or reparse point")
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(path_details.st_mode):
        raise CLIError("Reference image file must be a regular file")
    if not os.path.samestat(opened, path_details) or opened.st_size != path_details.st_size:
        raise CLIError("Reference image file changed while it was being read")


def _request_from_args(args: argparse.Namespace, prompt: str) -> GenerationRequest:
    reference_images = list(args.reference_image or ())
    reference_images.extend(
        _reference_image_data_uri(path)
        for path in (args.reference_image_file or ())
    )
    request = GenerationRequest(
        prompt=prompt,
        ratio=args.ratio,
        quality=args.quality,
        count=args.count,
        reference_images=tuple(reference_images),
    )
    if args.provider == "apimart":
        APIMartClient._validate_request(request)
    else:
        if request.reference_images:
            raise CLIError("Reference images are not supported by this provider")
        if not isinstance(request.ratio, str) or not request.ratio.strip():
            raise CLIError("Ratio must not be empty")
        if type(request.count) is not int or request.count <= 0:
            raise CLIError("Count must be a positive integer")
        quality_map = _custom_quality_map(args.custom_quality_map)
        if request.quality not in DEFAULT_QUALITY_MAP:
            raise CLIError("Unsupported quality")
        if quality_map is not None and request.quality not in quality_map:
            raise CLIError("Selected quality is absent from the declared custom quality map")
    return request


def _custom_quality_map(value: str | None) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CLIError("Custom quality map must not be empty")
    mapping: dict[str, str] = {}
    for entry in value.split(","):
        quality, separator, provider_value = entry.partition("=")
        quality = quality.strip()
        provider_value = provider_value.strip()
        if (
            separator != "="
            or quality not in _QUALITY_VALUES
            or not provider_value
            or quality in mapping
            or any(character.isspace() for character in provider_value)
        ):
            raise CLIError(
                "Custom quality map must use unique draft/standard/high=value entries"
            )
        mapping[quality] = provider_value
    return mapping


def _provider_configuration(args: argparse.Namespace) -> tuple[str, str | None]:
    if args.provider == "apimart":
        if (
            args.model is not None
            or args.api_key_env != "CUSTOM_IMAGE_API_KEY"
            or args.allow_local_http
            or args.custom_quality_map is not None
        ):
            raise CLIError("Options do not match the selected provider")
        base_url = args.base_url or "https://api.apimart.ai/v1"
        APIMartClient._validate_base_url(base_url)
        return "gpt-image-2", QUALITY_TO_RESOLUTION[args.quality]

    if not isinstance(args.base_url, str) or not isinstance(args.model, str):
        raise CLIError("Custom provider requires base URL and model")
    validate_base_url(args.base_url, allow_local_http=args.allow_local_http)
    if not args.model.strip():
        raise CLIError("Model must not be empty")
    if not _ENV_NAME_RE.fullmatch(args.api_key_env):
        raise CLIError("API key environment variable name is invalid")
    quality_map = _custom_quality_map(args.custom_quality_map)
    mapped_quality = None if quality_map is None else quality_map.get(args.quality)
    if quality_map is not None and mapped_quality is None:
        raise CLIError("Selected quality is absent from the declared custom quality map")
    return args.model, mapped_quality


def _providers_command(_: argparse.Namespace) -> dict[str, object]:
    return {
        "providers": [
            {
                "id": "codex",
                "agent_only": True,
                "cli_api_key": False,
                "quality": "high-quality-intent",
            },
            {
                "id": "apimart",
                "agent_only": False,
                "async": True,
                "key_environment": "APIMART_API_KEY",
                "quality_map": dict(QUALITY_TO_RESOLUTION),
            },
            {
                "id": "openai-compatible",
                "agent_only": False,
                "key_environment": "CUSTOM_IMAGE_API_KEY",
                "https_default": True,
                "approved_loopback_http": True,
                "quality": "omitted-unless-declared",
                "quality_map_option": "--custom-quality-map",
            },
        ]
    }


def _help_command(_: argparse.Namespace) -> dict[str, object]:
    return {
        "commands": ["providers", "brand", "dry-run", "generate", "help"],
        "usage": {
            "providers": "providers",
            "brand": "brand <list|show|add>",
            "dry_run": "dry-run --provider ... --prompt-file ... --ratio ... --quality ... --count ...",
            "generate": "generate --confirmed --provider ... --prompt-file ... --ratio ... --quality ... --count ...",
            "help": "help",
        },
    }


def _brand_command(args: argparse.Namespace) -> dict[str, object]:
    builtin_root, user_root = _brand_roots()
    if args.brand_command == "list":
        return {
            "brands": [
                _brand_payload(brand, include_asset=False)
                for brand in list_brands(builtin_root=builtin_root, user_root=user_root)
            ]
        }
    if args.brand_command == "show":
        brand = load_brand(args.name, builtin_root=builtin_root, user_root=user_root)
        return {"brand": _brand_payload(brand, include_asset=True)}
    brand = create_user_brand(
        args.name,
        Path(args.image),
        user_root=user_root,
        display_name=args.display_name,
        overwrite=args.overwrite,
    )
    return {"brand": _brand_payload(brand, include_asset=True)}


def _dry_run_command(args: argparse.Namespace) -> dict[str, object]:
    prompt = _read_prompt(args.prompt_file)
    model, mapped_quality = _provider_configuration(args)
    request = _request_from_args(args, prompt)
    return {
        "provider": args.provider,
        "model": model,
        "ratio": request.ratio,
        "quality": request.quality,
        "mapped_quality": mapped_quality,
        "quality_parameter": (
            "omitted"
            if args.provider == "openai-compatible" and mapped_quality is None
            else "declared-map"
            if args.provider == "openai-compatible"
            else "provider-native-map"
        ),
        "count": request.count,
        "prompt_characters": len(request.prompt),
        "has_reference_images": bool(request.reference_images),
    }


def _image_extension(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    raise CLIError("Image payload has an unsupported signature")


def _stage_bytes(data: bytes, destination_stem: Path) -> Path:
    if not isinstance(data, bytes) or not data or len(data) > MAX_DOWNLOAD_BYTES:
        raise CLIError("Image payload size is invalid")
    extension = _image_extension(data)
    target = destination_stem.with_suffix(extension)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}-",
            suffix=".part",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.replace(target)
        temporary = None
        return target
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _effective_brand(args: argparse.Namespace) -> str | None:
    if args.no_ip:
        return None
    brand_name = args.brand or "loki"
    builtin_root, user_root = _brand_roots()
    return load_brand(
        brand_name,
        builtin_root=builtin_root,
        user_root=user_root,
    ).id


def _generate_command(args: argparse.Namespace, correlation_id: str) -> dict[str, object]:
    if not args.confirmed:
        raise CLIError("Generation requires explicit confirmation")

    model, _ = _provider_configuration(args)
    topic = args.topic or "image"
    safe_slug(topic)
    brand = _effective_brand(args)
    prompt = _read_prompt(args.prompt_file)
    request = _request_from_args(args, prompt)
    local_task_id = uuid4().hex

    project_dir = Path.cwd().resolve()
    with tempfile.TemporaryDirectory(prefix=".loki-image2-", dir=project_dir) as staging_name:
        staging = Path(staging_name)
        images: list[Path] = []
        if args.provider == "apimart":
            client = APIMartClient(base_url=args.base_url or "https://api.apimart.ai/v1")
            result = client.generate(request)
            task_id = result.task_id
            if len(result.image_urls) != request.count:
                raise CLIError("Provider returned an unexpected image count")
            for index, url in enumerate(result.image_urls, start=1):
                images.append(download_image(url, staging / f"image-{index}"))
        else:
            client = OpenAICompatibleClient(
                args.base_url,
                args.model,
                api_key_env=args.api_key_env,
                allow_local_http=args.allow_local_http,
                quality_map=_custom_quality_map(args.custom_quality_map),
            )
            payloads = tuple(client.generate(request))
            task_id = local_task_id
            if len(payloads) != request.count:
                raise CLIError("Provider returned an unexpected image count")
            for index, payload in enumerate(payloads, start=1):
                if not isinstance(payload, ImagePayload):
                    raise CLIError("Provider returned an invalid image payload")
                if payload.url is not None:
                    images.append(download_image(
                        payload.url,
                        staging / f"image-{index}",
                        allow_local_http=args.allow_local_http,
                    ))
                elif payload.data is not None:
                    images.append(_stage_bytes(payload.data, staging / f"image-{index}"))
                else:
                    raise CLIError("Provider returned an empty image payload")

        output_dir = save_task_artifacts(
            project_dir,
            topic=topic,
            prompt=prompt,
            metadata={
                "brand": brand,
                "style": args.style,
                "ratio": request.ratio,
                "quality": request.quality,
                "provider": args.provider,
                "model": model,
                "task_id": task_id,
                "correlation_id": correlation_id,
            },
            images=images,
        )

    metadata = json.loads((output_dir / "metadata.json").read_text(encoding="utf-8"))
    return {
        "provider": args.provider,
        "task_id": task_id,
        "correlation_id": correlation_id,
        "output_dir": str(output_dir),
        "output_files": metadata["output_files"],
    }


def _add_generation_arguments(parser: argparse.ArgumentParser, *, confirmed: bool) -> None:
    if confirmed:
        parser.add_argument("--confirmed", action="store_true")
    parser.add_argument("--provider", choices=("apimart", "openai-compatible"), required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--ratio", required=True)
    parser.add_argument("--quality", choices=_QUALITY_VALUES, required=True)
    parser.add_argument("--count", type=int, required=True)
    references = parser.add_mutually_exclusive_group()
    references.add_argument("--reference-image", action="append", default=[])
    references.add_argument("--reference-image-file", action="append", default=[])
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env", default="CUSTOM_IMAGE_API_KEY")
    parser.add_argument("--allow-local-http", action="store_true")
    parser.add_argument("--custom-quality-map")
    if confirmed:
        identity = parser.add_mutually_exclusive_group()
        identity.add_argument("--brand")
        identity.add_argument("--no-ip", action="store_true")
        parser.add_argument("--style")
        parser.add_argument("--topic")


def _parser() -> JSONArgumentParser:
    parser = JSONArgumentParser(prog="loki_image2.py")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JSONArgumentParser)
    commands.add_parser("providers")
    commands.add_parser("help")

    brand = commands.add_parser("brand")
    brand_commands = brand.add_subparsers(
        dest="brand_command", required=True, parser_class=JSONArgumentParser
    )
    brand_commands.add_parser("list")
    show = brand_commands.add_parser("show")
    show.add_argument("name")
    add = brand_commands.add_parser("add")
    add.add_argument("name")
    add.add_argument("--image", required=True)
    add.add_argument("--display-name")
    add.add_argument("--overwrite", action="store_true")

    dry_run = commands.add_parser("dry-run")
    _add_generation_arguments(dry_run, confirmed=False)
    generate = commands.add_parser("generate")
    _add_generation_arguments(generate, confirmed=True)
    return parser


def _safe_error(
    exception: Exception,
    args: argparse.Namespace | None,
) -> tuple[str, str, dict[str, object]]:
    if isinstance(exception, CLIUsageError):
        return "usage_error", "Invalid command arguments", {}
    if isinstance(exception, CLIError):
        return "validation_error", str(exception), {}
    if isinstance(exception, AmbiguousSubmissionError):
        return (
            "ambiguous_submission",
            "提交结果未知；请先核查 Provider 任务与账单，不重投。",
            {
                "code": "ambiguous_submission",
                "billing_unknown": True,
                "retryable": False,
            },
        )
    if args is not None and args.command == "generate":
        return "generation_error", "Generation failed; see the error log", {}
    if args is not None and args.command == "dry-run":
        return "validation_error", "Dry run validation failed", {}
    if args is not None and args.command == "brand":
        return "brand_error", "Brand operation failed", {}
    return "internal_error", "Command failed", {}


def _emit(payload: dict[str, object], *, error: bool) -> None:
    stream = sys.stderr if error else sys.stdout
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    binary_stream = getattr(stream, "buffer", None)
    if binary_stream is not None:
        binary_stream.write(serialized.encode("utf-8"))
        binary_stream.flush()
    else:
        stream.write(serialized)


def main(argv: Sequence[str] | None = None) -> int:
    correlation_id = uuid4().hex
    args: argparse.Namespace | None = None
    try:
        effective_argv = list(sys.argv[1:] if argv is None else argv)
        if effective_argv == ["--help"]:
            effective_argv = ["help"]
        args = _parser().parse_args(effective_argv)
        _cleanup_existing_error_logs()
        if args.command == "help":
            result = _help_command(args)
        elif args.command == "providers":
            result = _providers_command(args)
        elif args.command == "brand":
            result = _brand_command(args)
        elif args.command == "dry-run":
            result = _dry_run_command(args)
        else:
            result = _generate_command(args, correlation_id)
        _emit({"ok": True, **result}, error=False)
        return 0
    except Exception as exception:
        error_type, message, machine_fields = _safe_error(exception, args)
        if args is not None and args.command == "generate" and getattr(args, "confirmed", False):
            try:
                runtime_root = _runtime_root()
                write_error_log(
                    runtime_root / "logs",
                    error_type=error_type,
                    provider=getattr(args, "provider", "unknown"),
                    summary="Generation command failed",
                    correlation_id=correlation_id,
                    trusted_root=runtime_root,
                )
            except Exception:
                pass
        _emit({
            "ok": False,
            "error_type": error_type,
            "message": message,
            "correlation_id": correlation_id,
            **machine_fields,
        }, error=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
