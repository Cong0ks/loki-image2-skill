"""Validated on-disk storage for built-in and user-created Loki brands."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
from pathlib import Path, PureWindowsPath
import shutil
import stat
import tempfile
from typing import Literal
from uuid import uuid4

from scripts.common import ensure_within, safe_slug


_IMAGE_TYPES = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".webp": "webp",
}


@dataclass(frozen=True)
class Brand:
    id: str
    display_name: str
    directory: Path
    character_image: Path
    anchors: Sequence[str]
    default_palette: Sequence[str]
    source: Literal["builtin", "user"]


def _is_reparse_point(path: Path) -> bool:
    """Identify symlinks and Windows reparse points without following them."""
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("Unable to validate brand path") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & reparse_flag
    )


def _trusted_path(root: Path, candidate: Path) -> Path:
    """Prove a path remains within root without crossing a reparse point."""
    supplied_root = Path(root)
    if _is_reparse_point(supplied_root):
        raise ValueError("Brand root is a link or reparse point")
    root = supplied_root.resolve()
    candidate = Path(candidate)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Brand path escapes the permitted root") from exc
    if _is_reparse_point(root):
        raise ValueError("Brand root is a link or reparse point")
    current = root
    for component in relative.parts:
        current /= component
        if _is_reparse_point(current):
            raise ValueError("Brand path contains a link or reparse point")
    try:
        return ensure_within(root, candidate)
    except ValueError as exc:
        raise ValueError("Brand path escapes the permitted root") from exc


def _character_filename(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("character_image must be a filename")
    path = Path(value)
    if (
        path.name != value
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
        or path.is_absolute()
        or PureWindowsPath(value).is_absolute()
    ):
        raise ValueError("character_image must be a single relative filename")
    if path.suffix.lower() not in _IMAGE_TYPES:
        raise ValueError("character_image has an unsupported format")
    return value


def _validate_image(path: Path) -> None:
    extension = path.suffix.lower()
    expected = _IMAGE_TYPES.get(extension)
    if expected is None:
        raise ValueError("Character image has an unsupported format")
    try:
        header = path.read_bytes()[:12]
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError("Unable to read character image") from exc

    if not header:
        raise ValueError("Character image is empty")
    valid = (
        expected == "png" and header.startswith(b"\x89PNG\r\n\x1a\n")
        or expected == "jpeg" and header.startswith(b"\xff\xd8\xff")
        or expected == "webp" and len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    )
    if not valid:
        raise ValueError("Character image extension does not match its signature")


def _string_sequence(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be an array of strings")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be an array of strings")
    return tuple(value)


def _parse_brand(
    directory: Path,
    source: Literal["builtin", "user"],
    root: Path,
    *,
    expected_id: str | None = None,
) -> Brand:
    directory = _trusted_path(root, directory)
    if not directory.is_dir():
        raise FileNotFoundError("Brand directory does not exist")
    metadata_path = _trusted_path(directory, directory / "brand.json")
    if not metadata_path.is_file():
        raise FileNotFoundError("Brand metadata does not exist")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Brand metadata is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Brand metadata has an unsupported schema")

    expected_id = safe_slug(directory.name) if expected_id is None else expected_id
    identifier = payload.get("id")
    display_name = payload.get("display_name")
    if not isinstance(identifier, str) or identifier != expected_id:
        raise ValueError("Brand metadata ID does not match its directory")
    if not isinstance(display_name, str) or not display_name.strip():
        raise ValueError("Brand display_name must be non-empty text")
    filename = _character_filename(payload.get("character_image"))
    image = _trusted_path(directory, directory / filename)
    if not image.is_file():
        raise FileNotFoundError("Brand character image does not exist")
    _validate_image(image)
    return Brand(
        id=identifier,
        display_name=display_name,
        directory=directory,
        character_image=image,
        anchors=_string_sequence(payload.get("anchors", ()), "anchors"),
        default_palette=_string_sequence(payload.get("default_palette", ()), "default_palette"),
        source=source,
    )


def _load_from_root(identifier: str, root: Path, source: Literal["builtin", "user"]) -> Brand | None:
    root = Path(root)
    if not root.exists():
        return None
    trusted_root = _trusted_path(root, root)
    if not trusted_root.is_dir():
        raise ValueError("Brand root is not a directory")
    directory = _trusted_path(trusted_root, trusted_root / identifier)
    if not directory.exists():
        return None
    return _parse_brand(directory, source, trusted_root)


def load_brand(name: str, *, builtin_root: Path, user_root: Path) -> Brand:
    """Load a brand, giving a valid user brand precedence over the built-in copy."""
    identifier = safe_slug(name)
    user_brand = _load_from_root(identifier, user_root, "user")
    if user_brand is not None:
        return user_brand
    builtin_brand = _load_from_root(identifier, builtin_root, "builtin")
    if builtin_brand is not None:
        return builtin_brand
    raise FileNotFoundError(f"Brand '{identifier}' does not exist")


def list_brands(*, builtin_root: Path, user_root: Path) -> list[Brand]:
    """Return all valid brands, ordered by ID, with user brands replacing built-ins."""
    brands: dict[str, Brand] = {}
    for root, source in ((Path(builtin_root), "builtin"), (Path(user_root), "user")):
        if not root.exists():
            continue
        trusted_root = _trusted_path(root, root)
        if not trusted_root.is_dir():
            raise ValueError("Brand root is not a directory")
        for directory in trusted_root.iterdir():
            if not directory.is_dir():
                continue
            brand = _parse_brand(directory, source, trusted_root)
            brands[brand.id] = brand
    return [brands[identifier] for identifier in sorted(brands)]


def _remove_directory(root: Path, directory: Path) -> None:
    """Remove a trusted directory without following links or reparse points."""
    directory = _trusted_path(root, directory)
    for child in directory.iterdir():
        if _is_reparse_point(child):
            raise ValueError("Refusing to remove a link or reparse point")
        if child.is_dir():
            _remove_directory(root, child)
        else:
            child.unlink()
    directory.rmdir()


def create_user_brand(
    name: str,
    image: Path,
    *,
    user_root: Path,
    display_name: str | None = None,
    anchors: Sequence[str] = (),
    palette: Sequence[str] = (),
    overwrite: bool = False,
) -> Brand:
    """Atomically create a validated user brand underneath ``user_root``."""
    identifier = safe_slug(name)
    resolved_display_name = name.strip() if display_name is None else display_name.strip()
    if not resolved_display_name:
        raise ValueError("display_name must be non-empty text")
    anchor_values = _string_sequence(anchors, "anchors")
    palette_values = _string_sequence(palette, "default_palette")

    source_image = Path(image)
    if not source_image.is_file():
        raise FileNotFoundError("Source character image does not exist")
    _validate_image(source_image)
    extension = source_image.suffix.lower()
    if extension == ".jpeg":
        extension = ".jpg"

    root = Path(user_root)
    root.mkdir(parents=True, exist_ok=True)
    root = _trusted_path(root, root)
    if not root.is_dir():
        raise ValueError("Brand root is not a directory")
    destination = _trusted_path(root, root / identifier)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Brand '{identifier}' already exists")

    staging = Path(tempfile.mkdtemp(prefix=f".brand-{identifier}-", dir=root))
    temporary: Path | None = None
    backup: Path | None = None
    try:
        staging = _trusted_path(root, staging)
        temporary = _trusted_path(root, staging / identifier)
        temporary.mkdir()
        character_name = f"character{extension}"
        character_path = _trusted_path(temporary, temporary / character_name)
        shutil.copyfile(source_image, character_path)
        payload = {
            "schema_version": 1,
            "id": identifier,
            "display_name": resolved_display_name,
            "character_image": character_name,
            "anchors": list(anchor_values),
            "default_palette": list(palette_values),
        }
        metadata_path = _trusted_path(temporary, temporary / "brand.json")
        metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Load the staged directory through the public parser before replacement.
        load_brand(identifier, builtin_root=staging, user_root=staging / ".no-user-brand")

        if destination.exists():
            _parse_brand(destination, "user", root)
            backup = _trusted_path(root, root / f".brand-backup-{identifier}-{uuid4().hex}")
            destination.rename(backup)
            try:
                temporary.rename(destination)
            except Exception:
                try:
                    backup.rename(destination)
                    backup = None
                except Exception as restore_error:
                    preserved_backup = backup
                    backup = None
                    raise ValueError(
                        "Brand replacement failed; the original brand remains "
                        f"recoverable at backup '{preserved_backup}'"
                    ) from restore_error
                raise
        else:
            temporary.rename(destination)
        temporary = None
        result = _parse_brand(destination, "user", root)
        if backup is not None:
            _remove_directory(root, backup)
            backup = None
        return result
    finally:
        if staging.exists():
            try:
                _remove_directory(root, staging)
            except (OSError, ValueError):
                pass
        if backup is not None and backup.exists():
            try:
                _remove_directory(root, backup)
            except (OSError, ValueError):
                pass
