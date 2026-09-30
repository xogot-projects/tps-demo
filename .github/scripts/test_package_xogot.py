import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from package_xogot import package


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.project = Path(self.directory.name) / "project"
        self.project.mkdir()
        (self.project / "project.godot").write_text(
            '[rendering]\ntextures/vram_compression/import_etc2_astc=true\n'
            'textures/vram_compression/import_s3tc_bptc=false\n'
        )
        imported = self.project / ".godot/imported"
        imported.mkdir(parents=True)
        (self.project / "texture.png").write_bytes(b"source texture")
        (imported / "texture.s3tc.ctex").write_bytes(b"desktop texture")
        (imported / "texture.etc2.ctex").write_bytes(b"mobile texture")
        paths = ["res://.godot/imported/texture.s3tc.ctex", "res://.godot/imported/texture.etc2.ctex"]
        (self.project / "texture.png.import").write_text(
            '[remap]\npath.s3tc="' + paths[0] + '"\npath.etc2="' + paths[1] + '"\n'
            'metadata={"imported_formats": ["s3tc_bptc", "etc2_astc"]}\n'
            '[deps]\nsource_file="res://texture.png"\ndest_files=' + json.dumps(paths) + '\n'
        )
        (imported / "texture.md5").write_text(
            'source_md5="' + hashlib.md5(b"source texture").hexdigest() + '"\n'
            'dest_md5="' + hashlib.md5(b"desktop texturemobile texture").hexdigest() + '"\n'
        )
        self.output = self.project / "build.zip"

    def test_retains_mobile_cache_and_normalizes_dependencies_without_editing_source(self):
        original = (self.project / "texture.png.import").read_bytes()
        for file in [".godot/editor/filesystem_cache10", ".godot/shader_cache/compiled", ".github/workflows/export.yaml", "build.sh"]:
            path = self.project / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"excluded")
        (self.project / ".godot/imported/model.scn").write_bytes(b"retained model")
        package(self.project, self.output)
        self.assertEqual((self.project / "texture.png.import").read_bytes(), original)
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            self.assertNotIn(".godot/imported/texture.s3tc.ctex", names)
            self.assertIn(".godot/imported/texture.etc2.ctex", names)
            self.assertIn(".godot/imported/model.scn", names)
            self.assertNotIn("build.zip", names)
            self.assertFalse(any(path.startswith((".godot/editor/", ".godot/shader_cache/", ".github/")) for path in names))
            text = archive.read("texture.png.import").decode()
            self.assertNotIn("s3tc", text)
            self.assertIn('"imported_formats": ["etc2_astc"]', text)
            self.assertIn(hashlib.md5(b"mobile texture").hexdigest(), archive.read(".godot/imported/texture.md5").decode())

    def test_stale_import_fails_before_upload_can_use_the_package(self):
        (self.project / ".godot/imported/texture.etc2.ctex").write_bytes(b"corrupt import")
        with self.assertRaisesRegex(ValueError, "Stale destination checksum"):
            package(self.project, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_mobile_cache_fails(self):
        (self.project / ".godot/imported/texture.etc2.ctex").unlink()
        with self.assertRaisesRegex(ValueError, "Missing imported dependency"):
            package(self.project, self.output)
        self.assertFalse(self.output.exists())

    def test_an_already_mobile_only_import_is_preserved(self):
        sidecar = self.project / "texture.png.import"
        sidecar.write_text(
            '[remap]\npath.etc2="res://.godot/imported/texture.etc2.ctex"\n'
            'metadata={"imported_formats": ["etc2_astc"]}\n'
            '[deps]\nsource_file="res://texture.png"\n'
            'dest_files=["res://.godot/imported/texture.etc2.ctex"]\n'
        )
        checksum = self.project / ".godot/imported/texture.md5"
        checksum.write_text(
            'source_md5="' + hashlib.md5(b"source texture").hexdigest() + '"\n'
            'dest_md5="' + hashlib.md5(b"mobile texture").hexdigest() + '"\n'
        )
        package(self.project, self.output)
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read("texture.png.import"), sidecar.read_bytes())
            self.assertEqual(archive.read(".godot/imported/texture.md5"), checksum.read_bytes())


if __name__ == "__main__":
    unittest.main()
