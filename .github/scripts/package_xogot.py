#!/usr/bin/env python3
"""Package preimported TPS assets for Xogot using ETC2/ASTC textures."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import zipfile


def dependency_path(project, resource):
    if not resource.startswith("res://"):
        raise ValueError(f"Not a project dependency: {resource}")
    relative = PurePosixPath(resource[6:])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe dependency: {resource}")
    path = project / str(relative)
    if not path.is_file():
        raise ValueError(f"Missing imported dependency: {resource}")
    return path


def digest_files(paths):
    # Godot's destination checksum hashes file contents in dependency order.
    digest = hashlib.md5()
    for path in paths:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def prepare_metadata(project):
    replacements = {}
    count = 0
    for sidecar in sorted(project.rglob("*.import")):
        relative = sidecar.relative_to(project).as_posix()
        if relative.startswith((".git/", ".godot/")):
            continue
        text = sidecar.read_text()
        if not re.search(r"^path\.s3tc=", text, re.MULTILINE):
            continue
        if not re.search(r"^path\.etc2=", text, re.MULTILINE):
            raise ValueError(f"No ETC2 replacement for {relative}")
        destinations = re.search(r"^dest_files=(.+)$", text, re.MULTILINE)
        if not destinations:
            raise ValueError(f"Missing destination list: {relative}")
        original = json.loads(destinations.group(1))
        retained = [path for path in original if ".s3tc." not in path]
        discarded = [path for path in original if ".s3tc." in path]
        if not retained or not discarded:
            raise ValueError(f"Unexpected texture destinations: {relative}")
        paths = [dependency_path(project, path) for path in original]
        checksum_path = paths[original.index(discarded[0])]
        checksum_path = checksum_path.with_name(checksum_path.name.split(".s3tc.")[0] + ".md5")
        checksums = checksum_path.read_text()
        destination_hash = re.search(r'^dest_md5="([^"]+)"', checksums, re.MULTILINE)
        source_hash = re.search(r'^source_md5="([^"]+)"', checksums, re.MULTILINE)
        source_name = re.search(r'^source_file="([^"]+)"', text, re.MULTILINE)
        if not destination_hash or not source_hash or not source_name:
            raise ValueError(f"Incomplete import checksums: {relative}")
        if digest_files(paths) != destination_hash.group(1):
            raise ValueError(f"Stale destination checksum: {relative}")
        source = dependency_path(project, source_name.group(1))
        if digest_files([source]) != source_hash.group(1):
            raise ValueError(f"Stale source checksum: {relative}")
        text = re.sub(r"^path\.s3tc=.*\n", "", text, flags=re.MULTILINE)
        formats = re.search(r'"imported_formats":\s*(\[[^\]]*\])', text)
        if not formats:
            raise ValueError(f"Missing imported format metadata: {relative}")
        formats_kept = [value for value in json.loads(formats.group(1)) if value != "s3tc_bptc"]
        if "etc2_astc" not in formats_kept:
            raise ValueError(f"Missing ETC2 format metadata: {relative}")
        text = text[:formats.start(1)] + json.dumps(formats_kept) + text[formats.end(1):]
        text = re.sub(r"^dest_files=.+$", "dest_files=" + json.dumps(retained), text, flags=re.MULTILINE)
        retained_hash = digest_files([dependency_path(project, path) for path in retained])
        checksums = re.sub(r'^dest_md5="[^"]+"', f'dest_md5="{retained_hash}"', checksums, flags=re.MULTILINE)
        replacements[relative] = text.encode()
        replacements[checksum_path.relative_to(project).as_posix()] = checksums.encode()
        count += 1
    return replacements, count


def package(project, output):
    project = project.resolve()
    output = output.resolve()
    settings = (project / "project.godot").read_text()
    if not re.search(r"^textures/vram_compression/import_etc2_astc=true$", settings, re.MULTILINE):
        raise ValueError("ETC2/ASTC imports must be enabled before packaging")
    replacements, count = prepare_metadata(project)
    key = "textures/vram_compression/import_s3tc_bptc"
    if re.search(rf"^{key}=", settings, re.MULTILINE):
        settings = re.sub(rf"^{key}=.*$", key + "=false", settings, flags=re.MULTILINE)
    else:
        settings = settings.replace("[rendering]\n", "[rendering]\n\n" + key + "=false\n", 1)
    replacements["project.godot"] = settings.encode()
    files = 0
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(project.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(project).as_posix()
                if path == output or relative.startswith((".git/", ".github/", ".vscode/", ".godot/editor/", ".godot/shader_cache/")):
                    continue
                if relative in (".git", ".gitignore", ".gitattributes", "build.sh", "build.zip"):
                    continue
                if relative.startswith(".godot/imported/") and ".s3tc." in relative:
                    continue
                info = zipfile.ZipInfo.from_file(path, arcname=relative)
                info.compress_type = zipfile.ZIP_DEFLATED
                if relative in replacements:
                    archive.writestr(info, replacements[relative])
                else:
                    with path.open("rb") as source, archive.open(info, "w") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                files += 1
        with zipfile.ZipFile(output) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"ZIP integrity failure: {bad}")
            names = set(archive.namelist())
            for relative in replacements:
                if not relative.endswith(".import"):
                    continue
                text = archive.read(relative).decode()
                paths = json.loads(re.search(r"^dest_files=(.+)$", text, re.MULTILINE).group(1))
                if any(path[6:] not in names for path in paths):
                    raise ValueError(f"Missing packaged dependency: {relative}")
    except Exception:
        output.unlink(missing_ok=True)
        raise
    print(f"Packaged {files} files; normalized {count} texture imports; {output.stat().st_size / 1048576:.1f} MiB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("build.zip"))
    arguments = parser.parse_args()
    package(arguments.project, arguments.output)
