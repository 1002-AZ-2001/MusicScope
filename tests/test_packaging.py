"""Vérifier le contenu des archives et les contrôles de publication sans gel coûteux."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest


spec = importlib.util.spec_from_file_location("build_release", Path(__file__).parents[1] / "scripts" / "build_release.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def bundle(tmp_path):
    result = tmp_path / "dist" / "MusicScope"
    (result / "_internal" / "musicscope" / "data").mkdir(parents=True)
    executable = result / "MusicScope"
    executable.write_bytes(b"native executable fixture")
    executable.chmod(0o755)
    (result / "LICENSE").write_text("AGPLv3", encoding="utf-8")
    (result / "_internal" / "musicscope" / "data" / "tone.flac").write_bytes(b"fLaCfixture")
    return result


def test_linux_archive_preserves_executable_and_whole_bundle(bundle, tmp_path):
    archive = builder.archive_bundle(bundle, tmp_path / "release", "0.2.0", "linux-x86_64")
    with tarfile.open(archive) as stream:
        if os.name != "nt":
            assert stream.getmember("MusicScope/MusicScope").mode & 0o111
        assert stream.extractfile("MusicScope/LICENSE").read() == b"AGPLv3"
        assert stream.extractfile("MusicScope/_internal/musicscope/data/tone.flac").read() == b"fLaCfixture"
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (archive.parent / "SHA256SUMS").read_text("ascii") == f"{expected}  {archive.name}\n"
    assert archive.with_name(archive.name + ".sha256").read_text("ascii").startswith(expected)


def test_linux_archive_retains_loader_symlinks(bundle, tmp_path):
    target = bundle / "_internal" / "libtone.so.1"
    target.write_bytes(b"ELFfixture")
    try:
        (bundle / "_internal" / "libtone.so").symlink_to("libtone.so.1")
    except OSError:
        pytest.skip("Liens symboliques indisponibles")
    archive = builder.archive_bundle(bundle, tmp_path / "release", "0.2.0", "linux-x86_64")
    with tarfile.open(archive) as stream:
        link = stream.getmember("MusicScope/_internal/libtone.so")
        assert link.issym() and link.linkname == "libtone.so.1"


def test_windows_binary_distribution_is_deferred(bundle, tmp_path):
    with pytest.raises(ValueError, match="Plateforme"):
        builder.archive_bundle(bundle, tmp_path / "release", "0.2.0", "windows-x86_64")
    with pytest.raises(ValueError, match="Linux"):
        builder.smoke_test(bundle, system="Windows")


def test_checksums_include_linux_formats_and_ignore_sidecars(bundle, tmp_path):
    directory = tmp_path / "release"
    linux = builder.archive_bundle(bundle, directory, "0.2.0", "linux-x86_64")
    debian = directory / "musicscope_0.2.0-1_amd64.deb"
    debian.write_bytes(b"debian package fixture")
    arch = directory / "musicscope-0.2.0-1-x86_64.pkg.tar.zst"
    arch.write_bytes(b"arch package fixture")
    (directory / "stale-windows.zip").write_bytes(b"ignored deferred target")
    lines = builder.write_checksums(directory).read_text("ascii").splitlines()
    assert len(lines) == 3
    assert {line.split("  ")[1] for line in lines} == {linux.name, debian.name, arch.name}


def test_release_tag_must_match_version():
    builder.check_release_tag("0.2.0", {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v0.2.0"})
    builder.check_release_tag("0.2.0", {"GITHUB_REF_TYPE": "branch", "GITHUB_REF_NAME": "main"})
    with pytest.raises(ValueError, match="tag"):
        builder.check_release_tag("0.2.0", {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "v9.0.0"})


def test_version_metadata_disagreement_refuses_release(tmp_path):
    (tmp_path / "musicscope").mkdir()
    (tmp_path / "musicscope" / "__init__.py").write_text('__version__ = "0.2.0"\n')
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    with pytest.raises(ValueError, match="versions"):
        builder.project_version(tmp_path)


@pytest.mark.parametrize("system,machine,expected", [
    ("Linux", "x86_64", "linux-x86_64"), ("Linux", "AMD64", "linux-x86_64"),
])
def test_platform_names(system, machine, expected):
    assert builder.platform_label(system, machine) == expected


def test_unrecognized_platform_or_invalid_archive_name_is_rejected(bundle, tmp_path):
    with pytest.raises(ValueError):
        builder.platform_label("Darwin", "arm64")
    with pytest.raises(ValueError):
        builder.platform_label("Windows", "AMD64")
    with pytest.raises(ValueError):
        builder.platform_label("Linux", "arm64")
    with pytest.raises(ValueError):
        builder.archive_bundle(bundle, tmp_path, "../../elsewhere", "linux-x86_64")
    with pytest.raises(ValueError):
        builder.archive_bundle(bundle, tmp_path, "0.2.0", "linux/elsewhere")


def test_frozen_smoke_uses_clean_external_working_directory(bundle, monkeypatch):
    calls = []
    monkeypatch.setenv("QT_PLUGIN_PATH", "/unrelated/qt")
    monkeypatch.setenv("QT_QPA_PLATFORM_PLUGIN_PATH", "/unrelated/qt/platforms")
    monkeypatch.setattr(builder.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    builder.smoke_test(bundle, system="Linux")
    assert len(calls) == 2
    assert calls[0][0][-1] == "--help"
    assert calls[1][0][-1] == "--self-test"
    assert calls[1][1]["check"] is True
    assert calls[1][1]["env"]["QT_QPA_PLATFORM"] == "offscreen"
    assert "QT_PLUGIN_PATH" not in calls[1][1]["env"]
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" not in calls[1][1]["env"]
    assert not Path(calls[1][1]["cwd"]).is_relative_to(bundle)


def test_real_debian_package_layout_metadata_and_permissions(bundle, tmp_path):
    if not shutil.which("dpkg-deb"):
        pytest.skip("dpkg-deb indisponible")
    archive = builder.make_debian_package(bundle, tmp_path / "release", "0.2.0", minimum_libc="2.36")
    control = subprocess.run(["dpkg-deb", "--field", str(archive)], check=True, capture_output=True, text=True).stdout
    assert "Package: musicscope" in control
    assert "Architecture: amd64" in control
    assert "libc6 (>= 2.36)" in control
    assert "libglib2.0-0" in control
    target = tmp_path / "extracted"
    subprocess.run(["dpkg-deb", "--extract", str(archive), str(target)], check=True)
    launcher = target / "usr/bin/musicscope"
    assert 'exec /opt/musicscope/MusicScope "$@"' in launcher.read_text("utf-8")
    assert launcher.stat().st_mode & 0o111
    assert (target / "opt/musicscope/_internal/musicscope/data/tone.flac").read_bytes() == b"fLaCfixture"
    assert (target / "opt/musicscope/LICENSE").read_text("utf-8") == "AGPLv3"
    assert "Exec=/usr/bin/musicscope" in (target / "usr/share/applications/musicscope.desktop").read_text("utf-8")
    assert (target / "usr/share/icons/hicolor/scalable/apps/musicscope.svg").is_file()
    assert (target / "usr/share/doc/musicscope/copyright").is_file()
    assert archive.name in (archive.parent / "SHA256SUMS").read_text("ascii")


def test_package_refuses_escaping_link_or_injected_metadata(bundle, tmp_path):
    with pytest.raises(ValueError, match="glibc"):
        builder.make_debian_package(bundle, tmp_path / "release", "0.2.0", minimum_libc="2.36\nConflicts: all")
    outside = tmp_path / "outside"
    outside.write_bytes(b"not part of app")
    try:
        (bundle / "escape").symlink_to(outside)
    except OSError:
        pytest.skip("Liens indisponibles")
    with pytest.raises(ValueError, match="sortant"):
        builder.make_debian_package(bundle, tmp_path / "release", "0.2.0", minimum_libc="2.36")
    assert not (tmp_path / "release").exists()


def test_arch_recipe_verifies_all_local_sources_and_preserves_runtime(bundle, tmp_path):
    archive = builder.archive_bundle(bundle, tmp_path / "release", "0.2.0", "linux-x86_64")
    recipe = builder.prepare_arch_recipe(archive, tmp_path / "arch", "0.2.0", minimum_libc="2.36")
    text = recipe.read_text("utf-8")
    assert "'glibc>=2.36'" in text
    assert "options=('!strip' '!debug')" in text
    assert "AGPL-3.0-only" in text
    assert '"$pkgdir/opt/musicscope/"' in text
    assert "sha256sums=" in text and "SKIP" not in text
    for path in recipe.parent.iterdir():
        if path != recipe:
            assert hashlib.sha256(path.read_bytes()).hexdigest() in text
    assert (recipe.parent / "musicscope-launcher").is_file()
    assert (recipe.parent / "musicscope.svg").is_file()
    subprocess.run(["bash", "-n", str(recipe)], check=True)


def test_libc_floor_is_detected_and_unknown_libc_rejected(monkeypatch):
    monkeypatch.setattr(builder.platform, "libc_ver", lambda: ("glibc", "2.36"))
    assert builder.libc_floor() == "2.36"
    monkeypatch.setattr(builder.platform, "libc_ver", lambda: ("musl", "1.2"))
    with pytest.raises(ValueError, match="glibc"):
        builder.libc_floor()
