from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT))

from scripts.brand_store import create_user_brand, list_brands, load_brand


TEST_TEMP_ROOT = SKILL_ROOT / ".test-tmp"
TEST_TEMP_ROOT.mkdir(exist_ok=True)
PNG = b"\x89PNG\r\n\x1a\nminimal-payload"
JPEG = b"\xff\xd8\xffminimal-payload"
WEBP = b"RIFF\x00\x00\x00\x00WEBPminimal-payload"


@contextmanager
def writable_temporary_directory():
    """Use a workspace-local temporary directory with a Windows-safe ACL."""
    original_mkdir = os.mkdir

    def mkdir_with_workspace_acl(path, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            return original_mkdir(path, 0o777)
        return original_mkdir(path, 0o777, dir_fd=dir_fd)

    with patch.object(tempfile._os, "mkdir", side_effect=mkdir_with_workspace_acl):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as directory:
            yield Path(directory)


def write_brand(root: Path, name: str, *, image_name: str = "character.png", image: bytes = PNG,
                schema_version: int = 1, metadata_id: str | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / image_name).write_bytes(image)
    (directory / "brand.json").write_text(
        json.dumps({
            "schema_version": schema_version,
            "id": metadata_id if metadata_id is not None else name,
            "display_name": f"{name} display",
            "character_image": image_name,
            "anchors": ["anchor"],
            "default_palette": ["#ffffff"],
        }),
        encoding="utf-8",
    )
    return directory


class BrandStoreTests(unittest.TestCase):
    def make_roots(self, base: Path) -> tuple[Path, Path]:
        builtin = base / "builtin"
        user = base / "user"
        builtin.mkdir()
        user.mkdir()
        return builtin, user

    def test_user_brand_overrides_builtin_with_same_id(self):
        with writable_temporary_directory() as temp:
            builtin, user = self.make_roots(temp)
            write_brand(builtin, "loki")
            write_brand(user, "loki")

            brand = load_brand("Loki", builtin_root=builtin, user_root=user)

            self.assertEqual(brand.source, "user")
            self.assertEqual(brand.directory, (user / "loki").resolve())

    def test_list_brands_is_unique_and_sorted(self):
        with writable_temporary_directory() as temp:
            builtin, user = self.make_roots(temp)
            write_brand(builtin, "zeta")
            write_brand(builtin, "alpha")
            write_brand(user, "zeta")
            write_brand(user, "beta")

            brands = list_brands(builtin_root=builtin, user_root=user)

            self.assertEqual([brand.id for brand in brands], ["alpha", "beta", "zeta"])
            self.assertEqual(brands[-1].source, "user")

    def test_create_copies_image_and_writes_relative_asset_path(self):
        with writable_temporary_directory() as temp:
            _, user = self.make_roots(temp)
            source = temp / "input.PNG"
            source.write_bytes(PNG)

            brand = create_user_brand("  My Hero  ", source, user_root=user)
            payload = json.loads((brand.directory / "brand.json").read_text(encoding="utf-8"))

            self.assertEqual(brand.id, "my-hero")
            self.assertEqual(brand.display_name, "My Hero")
            self.assertTrue((brand.directory / "character.png").is_file())
            self.assertEqual(payload["character_image"], "character.png")
            self.assertNotIn("/", payload["character_image"])
            self.assertNotIn("\\", payload["character_image"])

    def test_duplicate_requires_explicit_overwrite(self):
        with writable_temporary_directory() as temp:
            _, user = self.make_roots(temp)
            source = temp / "input.png"
            source.write_bytes(PNG)
            create_user_brand("hero", source, user_root=user, display_name="old")

            with self.assertRaises(FileExistsError):
                create_user_brand("hero", source, user_root=user, display_name="new")
            replacement = create_user_brand(
                "hero", source, user_root=user, display_name="new", overwrite=True
            )

            self.assertEqual(replacement.display_name, "new")

    def test_failed_overwrite_restores_previous_brand(self):
        with writable_temporary_directory() as temp:
            _, user = self.make_roots(temp)
            source = temp / "input.png"
            source.write_bytes(PNG)
            create_user_brand("hero", source, user_root=user, display_name="original")
            original_rename = Path.rename

            def fail_final_rename(source_path, destination, *args, **kwargs):
                if (
                    Path(source_path).name == "hero"
                    and Path(source_path).parent.name.startswith(".brand-hero-")
                    and Path(destination) == user / "hero"
                ):
                    raise OSError("injected final rename failure")
                return original_rename(source_path, destination, *args, **kwargs)

            with patch.object(Path, "rename", autospec=True, side_effect=fail_final_rename):
                with self.assertRaises(OSError):
                    create_user_brand("hero", source, user_root=user, display_name="replacement", overwrite=True)

            self.assertEqual(
                load_brand("hero", builtin_root=temp / "builtin", user_root=user).display_name,
                "original",
            )

    def test_failed_overwrite_preserves_backup_when_restore_also_fails(self):
        with writable_temporary_directory() as temp:
            _, user = self.make_roots(temp)
            source = temp / "input.png"
            source.write_bytes(PNG)
            create_user_brand("hero", source, user_root=user, display_name="original")
            original_rename = Path.rename
            backups: list[Path] = []

            def fail_install_and_restore(source_path, destination, *args, **kwargs):
                source_path = Path(source_path)
                destination = Path(destination)
                if source_path == user / "hero" and destination.parent == user:
                    backups.append(destination)
                if destination == user / "hero" and (
                    source_path.parent.name.startswith(".brand-hero-")
                    or source_path.name.startswith(".brand-backup-hero-")
                ):
                    raise OSError("injected rename failure")
                return original_rename(source_path, destination, *args, **kwargs)

            with patch.object(Path, "rename", autospec=True, side_effect=fail_install_and_restore):
                with self.assertRaisesRegex(ValueError, "backup") as raised:
                    create_user_brand("hero", source, user_root=user, display_name="replacement", overwrite=True)

            self.assertEqual(len(backups), 1)
            backup = backups[0]
            self.assertIn(str(backup), str(raised.exception))
            self.assertTrue((backup / "brand.json").is_file())
            self.assertEqual((backup / "character.png").read_bytes(), PNG)
            backup_payload = json.loads((backup / "brand.json").read_text(encoding="utf-8"))
            self.assertEqual(backup_payload["display_name"], "original")

    def test_traversal_brand_name_is_rejected(self):
        with writable_temporary_directory() as temp:
            _, user = self.make_roots(temp)
            source = temp / "input.png"
            source.write_bytes(PNG)
            for name in ("../evil", "..\\evil", "C:\\evil", "\\rooted"):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        create_user_brand(name, source, user_root=user)

    def test_missing_and_fake_image_are_rejected(self):
        with writable_temporary_directory() as temp:
            builtin, user = self.make_roots(temp)
            write_brand(builtin, "missing")
            (builtin / "missing" / "character.png").unlink()
            write_brand(user, "fake", image=b"not an image")

            with self.assertRaises(FileNotFoundError):
                load_brand("missing", builtin_root=builtin, user_root=user)
            with self.assertRaises(ValueError):
                load_brand("fake", builtin_root=builtin, user_root=user)

    def test_png_jpeg_and_webp_signatures_are_accepted(self):
        with writable_temporary_directory() as temp:
            builtin, user = self.make_roots(temp)
            for name, image_name, payload in (
                ("png", "character.png", PNG),
                ("jpeg", "character.jpg", JPEG),
                ("webp", "character.webp", WEBP),
            ):
                write_brand(builtin, name, image_name=image_name, image=payload)

            for name in ("png", "jpeg", "webp"):
                with self.subTest(name=name):
                    self.assertEqual(load_brand(name, builtin_root=builtin, user_root=user).id, name)

    def test_signature_extension_mismatch_is_rejected(self):
        with writable_temporary_directory() as temp:
            builtin, user = self.make_roots(temp)
            write_brand(builtin, "wrong", image_name="character.png", image=JPEG)

            with self.assertRaises(ValueError):
                load_brand("wrong", builtin_root=builtin, user_root=user)

    def test_character_path_escape_or_reparse_is_rejected(self):
        with writable_temporary_directory() as temp:
            builtin, user = self.make_roots(temp)
            escaped = write_brand(builtin, "escaped", image_name="../outside.png")
            drive_relative = write_brand(builtin, "drive-relative")
            drive_payload = json.loads((drive_relative / "brand.json").read_text(encoding="utf-8"))
            drive_payload["character_image"] = "C:outside.png"
            (drive_relative / "brand.json").write_text(json.dumps(drive_payload), encoding="utf-8")
            outside = temp / "outside.png"
            outside.write_bytes(PNG)
            reparse = write_brand(builtin, "reparse")
            original_lstat = Path.lstat

            def lstat_with_reparse(path, *args, **kwargs):
                details = original_lstat(path, *args, **kwargs)
                if Path(path) == reparse / "character.png":
                    return type("Details", (), {
                        "st_mode": details.st_mode,
                        "st_file_attributes": 0x400,
                    })()
                return details

            with self.assertRaises(ValueError):
                load_brand("escaped", builtin_root=builtin, user_root=user)
            with self.assertRaises(ValueError):
                load_brand("drive-relative", builtin_root=builtin, user_root=user)
            with patch.object(Path, "lstat", autospec=True, side_effect=lstat_with_reparse):
                with self.assertRaises(ValueError):
                    load_brand("reparse", builtin_root=builtin, user_root=user)

    def test_invalid_schema_or_id_mismatch_is_rejected(self):
        with writable_temporary_directory() as temp:
            builtin, user = self.make_roots(temp)
            write_brand(builtin, "schema", schema_version=2)
            write_brand(builtin, "identifier", metadata_id="other")

            for name in ("schema", "identifier"):
                with self.subTest(name=name):
                    with self.assertRaises(ValueError):
                        load_brand(name, builtin_root=builtin, user_root=user)

    def test_brand_json_contains_no_secret_fields(self):
        with writable_temporary_directory() as temp:
            _, user = self.make_roots(temp)
            source = temp / "input.jpeg"
            source.write_bytes(JPEG)

            brand = create_user_brand("hero", source, user_root=user, anchors=("face",), palette=("#123456",))
            payload = json.loads((brand.directory / "brand.json").read_text(encoding="utf-8"))

            self.assertEqual(
                payload,
                {
                    "schema_version": 1,
                    "id": "hero",
                    "display_name": "hero",
                    "character_image": "character.jpg",
                    "anchors": ["face"],
                    "default_palette": ["#123456"],
                },
            )
            self.assertFalse({"api_key", "apikey", "authorization", "token", "secret"} & set(payload))


if __name__ == "__main__":
    unittest.main()
