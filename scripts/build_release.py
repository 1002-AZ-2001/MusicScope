"""Construire et tester un paquet natif, sans publication ni téléchargement ad hoc.

Lancer sous Linux x86-64 avec l'interpréteur du venv contenant l'extra ``build``.
Les formats produits ciblent Debian et Arch Linux ; Windows est différé.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
from importlib.metadata import distribution
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def project_version(root: Path = ROOT) -> str:
    """Lire la version sans importer Qt ni exécuter du code du projet."""
    version = None
    tree = ast.parse((root / "musicscope" / "__init__.py").read_text("utf-8"))
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            version = ast.literal_eval(statement.value)
    declared = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+[a-zA-Z0-9.+-]*", version):
        raise ValueError("Version applicative absente ou non valide.")
    if version != declared:
        raise ValueError("Les versions de pyproject.toml et musicscope/__init__.py diffèrent.")
    return version


def platform_label(system: str | None = None, machine: str | None = None) -> str:
    system, machine = system or platform.system(), machine or platform.machine()
    architecture = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(machine.lower())
    if system != "Linux" or architecture != "x86_64":
        raise ValueError(f"Plateforme de distribution non prise en charge : {system}/{machine}.")
    return f"{system.lower()}-{architecture}"


def check_release_tag(version: str, environ=None) -> None:
    environ = os.environ if environ is None else environ
    if environ.get("GITHUB_REF_TYPE") == "tag" and environ.get("GITHUB_REF_NAME") != f"v{version}":
        raise ValueError("Le tag de publication ne correspond pas à la version applicative.")


def write_checksums(directory: Path) -> Path:
    """Recalculer les sommes des seules archives qui seront publiées."""
    archives = sorted([*directory.glob("*.tar.gz"), *directory.glob("*.deb"), *directory.glob("*.pkg.tar.zst")])
    if not archives:
        raise ValueError("Aucune archive à vérifier.")
    target = directory / "SHA256SUMS"
    lines = []
    for archive in archives:
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        lines.append(f"{digest}  {archive.name}\n")
    target.write_text("".join(lines), encoding="ascii")
    return target


def archive_bundle(bundle: Path, output: Path, version: str, label: str) -> Path:
    if not bundle.is_dir():
        raise ValueError(f"Bundle absent : {bundle}")
    if not re.fullmatch(r"\d+\.\d+\.\d+[a-zA-Z0-9.+-]*", version):
        raise ValueError("Version d'archive non valide.")
    if label != "linux-x86_64":
        raise ValueError("Plateforme d'archive non valide.")
    output.mkdir(parents=True, exist_ok=True)
    stem = f"MusicScope-{version}-{label}"
    archive = output / f"{stem}.tar.gz"
    # tar conserve droits d'exécution et liens requis par le chargeur ELF.
    with tarfile.open(archive, "w:gz", dereference=False) as stream:
        stream.add(bundle, arcname="MusicScope")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    write_checksums(output)
    return archive


def smoke_test(bundle: Path, system: str | None = None) -> None:
    system = system or platform.system()
    if system != "Linux":
        raise ValueError("Les exécutables de cette version ciblent Linux uniquement.")
    executable = bundle / "MusicScope"
    if not executable.is_file():
        raise ValueError(f"Exécutable CLI absent : {executable}")
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONUTF8"] = "1"
    # Ne pas transmettre au paquet gelé des chemins de plugins d'un Qt externe.
    environment.pop("QT_PLUGIN_PATH", None)
    environment.pop("QT_QPA_PLATFORM_PLUGIN_PATH", None)
    with tempfile.TemporaryDirectory(prefix="musicscope-frozen-") as temporary:
        environment["XDG_CACHE_HOME"] = str(Path(temporary) / "cache")
        for arguments in (("--help",), ("--cache", str(Path(temporary) / "app-cache"), "--self-test")):
            subprocess.run([str(executable.resolve()), *arguments], check=True, timeout=120,
                           env=environment, cwd=temporary)


DEBIAN_DEPENDS = [
    "libstdc++6", "libglib2.0-0", "libgl1", "libegl1", "libopengl0", "libx11-6",
    "libxkbcommon0", "libxkbcommon-x11-0", "fontconfig", "libfreetype6",
    "libdbus-1-3", "libxcb1", "libxcb-cursor0", "libxcb-icccm4",
    "libxcb-keysyms1", "libxcb-image0", "libxcb-render-util0", "libxcb-xinerama0", "libxcb-shape0", "zlib1g",
]
ARCH_DEPENDS = [
    "gcc-libs", "glib2", "libglvnd", "libx11", "libxkbcommon", "libxkbcommon-x11", "fontconfig", "freetype2",
    "dbus", "libxcb", "xcb-util-cursor", "xcb-util-wm", "xcb-util-keysyms",
    "xcb-util-image", "xcb-util-renderutil", "zlib",
]


def libc_floor() -> str:
    """Ne pas annoncer une compatibilité plus ancienne que l'hôte de build."""
    name, version = platform.libc_ver()
    if name != "glibc" or not re.fullmatch(r"\d+\.\d+", version):
        raise ValueError("La version glibc de construction est indéterminée.")
    return version


def _check_package_inputs(bundle: Path, version: str, minimum_libc: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+[a-zA-Z0-9.+-]*", version):
        raise ValueError("Version de paquet non valide.")
    if not re.fullmatch(r"\d+\.\d+", minimum_libc):
        raise ValueError("Version de glibc non valide.")
    if not (bundle / "MusicScope").is_file() or not (bundle / "LICENSE").is_file():
        raise ValueError("Bundle incomplet : exécutable et licence requis.")
    resolved = bundle.resolve()
    for path in bundle.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(resolved):
            raise ValueError(f"Lien sortant du bundle refusé : {path}")


def _normalize_modes(tree: Path) -> None:
    tree.chmod(0o755)
    for path in tree.rglob("*"):
        if path.is_symlink():
            continue
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)


def make_debian_package(bundle: Path, output: Path, version: str,
                        minimum_libc: str | None = None, root: Path = ROOT) -> Path:
    """Créer un vrai paquet dpkg sans installation sur la machine de build."""
    minimum_libc = minimum_libc or libc_floor()
    _check_package_inputs(bundle, version, minimum_libc)
    if not shutil.which("dpkg-deb"):
        raise ValueError("dpkg-deb est requis pour construire le paquet Debian.")
    output.mkdir(parents=True, exist_ok=True)
    archive = output.resolve() / f"musicscope_{version}-1_amd64.deb"
    assets = root / "scripts" / "build_assets"
    with tempfile.TemporaryDirectory(prefix="musicscope-deb-") as temporary:
        stage = Path(temporary)
        payload = stage / "opt" / "musicscope"
        shutil.copytree(bundle, payload, symlinks=True)
        for source, destination in [
            (assets / "musicscope-launcher", stage / "usr/bin/musicscope"),
            (assets / "musicscope.desktop", stage / "usr/share/applications/musicscope.desktop"),
            (assets / "musicscope.svg", stage / "usr/share/icons/hicolor/scalable/apps/musicscope.svg"),
            (bundle / "LICENSE", stage / "usr/share/doc/musicscope/copyright"),
        ]:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        (stage / "usr/bin/musicscope").chmod(0o755)
        control = stage / "DEBIAN"
        control.mkdir()
        size = sum(path.stat().st_size for path in stage.rglob("*") if path.is_file() and not path.is_symlink())
        (control / "control").write_text(
            f"Package: musicscope\nVersion: {version}-1\nArchitecture: amd64\n"
            "Section: sound\nPriority: optional\nMaintainer: MusicScope contributors\n"
            f"Installed-Size: {(size + 1023) // 1024}\n"
            f"Depends: libc6 (>= {minimum_libc}), {', '.join(DEBIAN_DEPENDS)}\n"
            "Suggests: libchromaprint-tools\n"
            "Homepage: https://github.com/1002-AZ-2001/MusicScope\n"
            "Description: Inventaire, comparaison et audit de musique pour Jellyfin\n"
            " Application Qt en francais avec cache local, comparaison d'albums,\n"
            " verification MusicBrainz et rangement assiste avec restauration.\n",
            encoding="utf-8",
        )
        checksums = []
        for path in sorted(stage.rglob("*")):
            if path.is_file() and not path.is_symlink() and not path.is_relative_to(control):
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, lambda: hashlib.md5(usedforsecurity=False)).hexdigest()
                checksums.append(f"{digest}  {path.relative_to(stage).as_posix()}\n")
        (control / "md5sums").write_text("".join(checksums), encoding="utf-8")
        _normalize_modes(stage)
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", "--uniform-compression",
                        "-Zxz", "-z3", str(stage), str(archive)], check=True)
    write_checksums(output)
    return archive


def prepare_arch_recipe(portable: Path, directory: Path, version: str,
                        minimum_libc: str | None = None, root: Path = ROOT) -> Path:
    """Préparer les sources locales vérifiées pour un vrai makepkg non-root."""
    minimum_libc = minimum_libc or libc_floor()
    if not re.fullmatch(r"\d+\.\d+\.\d+[a-zA-Z0-9.+-]*", version):
        raise ValueError("Version de paquet Arch non valide.")
    if not re.fullmatch(r"\d+\.\d+", minimum_libc):
        raise ValueError("Version de glibc non valide.")
    if not portable.is_file() or portable.name != f"MusicScope-{version}-linux-x86_64.tar.gz":
        raise ValueError("Archive portable attendue absente ou mal nommée.")
    directory.mkdir(parents=True, exist_ok=True)
    sources = [portable, *[(root / "scripts/build_assets" / name)
                          for name in ("musicscope-launcher", "musicscope.desktop", "musicscope.svg")]]
    hashes = []
    for source in sources:
        target = directory / source.name
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        with source.open("rb") as stream:
            hashes.append(hashlib.file_digest(stream, "sha256").hexdigest())
    source_names = " ".join(f"'{source.name}'" for source in sources)
    hash_values = " ".join(f"'{value}'" for value in hashes)
    dependencies = " ".join(f"'{value}'" for value in [f"glibc>={minimum_libc}", *ARCH_DEPENDS])
    recipe = directory / "PKGBUILD"
    recipe.write_text(
        "# Paquet binaire natif ; généré depuis un bundle testé et vérifié.\n"
        "pkgname=musicscope\n"
        f"pkgver={version}\npkgrel=1\n"
        "pkgdesc='Inventaire, comparaison et audit de musique pour Jellyfin'\n"
        "arch=('x86_64')\nurl='https://github.com/1002-AZ-2001/MusicScope'\n"
        "license=('AGPL-3.0-only')\n"
        f"depends=({dependencies})\n"
        "optdepends=('chromaprint: empreintes audio locales avec fpcalc')\n"
        "options=('!strip' '!debug')\n"
        f"source=({source_names})\nsha256sums=({hash_values})\n"
        "\npackage() {\n"
        "  install -d \"$pkgdir/opt/musicscope\"\n"
        "  cp -a \"$srcdir/MusicScope/.\" \"$pkgdir/opt/musicscope/\"\n"
        "  find \"$pkgdir/opt/musicscope\" -type d -exec chmod 755 {} +\n"
        "  find \"$pkgdir/opt/musicscope\" -type f ! -perm /111 -exec chmod 644 {} +\n"
        "  install -Dm755 \"$srcdir/musicscope-launcher\" \"$pkgdir/usr/bin/musicscope\"\n"
        "  install -Dm644 \"$srcdir/musicscope.desktop\" \"$pkgdir/usr/share/applications/musicscope.desktop\"\n"
        "  install -Dm644 \"$srcdir/musicscope.svg\" \"$pkgdir/usr/share/icons/hicolor/scalable/apps/musicscope.svg\"\n"
        "  install -Dm644 \"$srcdir/MusicScope/LICENSE\" \"$pkgdir/usr/share/licenses/musicscope/LICENSE\"\n"
        "}\n", encoding="utf-8",
    )
    return recipe


def copy_third_party_notices(bundle: Path, root: Path = ROOT) -> None:
    """Inclure les textes des licences et versions des dépendances redistribuées."""
    licenses = bundle / "licenses"
    licenses.mkdir(exist_ok=True)
    for source in (root / "scripts" / "build_licenses").glob("*.txt"):
        shutil.copyfile(source, licenses / source.name)
    python_licenses = [Path(sys.base_prefix) / "LICENSE.txt", Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"]
    python_license = next((path for path in python_licenses if path.is_file()), None)
    if python_license is None:
        raise ValueError("Le texte de la licence Python n'a pas été trouvé ; distribution interrompue.")
    shutil.copyfile(python_license, licenses / "PYTHON-LICENSE.txt")
    mutagen = distribution("mutagen")
    mutagen_license = next((mutagen.locate_file(path) for path in mutagen.files or []
                            if path.name == "COPYING"), None)
    if mutagen_license is None:
        raise ValueError("Le texte de la licence Mutagen n'a pas été trouvé ; distribution interrompue.")
    shutil.copyfile(mutagen_license, licenses / "MUTAGEN-COPYING.txt")
    pyside = distribution("PySide6").version
    shiboken = distribution("shiboken6").version
    # Qt est lié dynamiquement dans le dossier _internal, pas incorporé à l'EXE.
    (bundle / "THIRD_PARTY.txt").write_text(
        f"Python {platform.python_version()} — Python Software Foundation License\n"
        "Sources : https://www.python.org/downloads/source/\n\n"
        f"Qt / PySide6 {pyside}, shiboken6 {shiboken} — LGPLv3 / GPLv3 selon composant\n"
        "Textes LGPLv3 et GPLv3 : dossier licenses. Bibliothèques dynamiques : _internal.\n"
        "Vous pouvez remplacer les bibliothèques LGPL par des versions compatibles et analyser\n"
        "leur fonctionnement pour déboguer ces modifications, conformément à la LGPL.\n"
        f"Sources PySide : https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v{pyside}\n"
        f"Sources Qt : https://download.qt.io/official_releases/qt/{'.'.join(pyside.split('.')[:2])}/{pyside}/single/\n"
        "Notices des composants Qt : https://doc.qt.io/qt-6/licenses-used-in-qt.html\n\n"
        f"Mutagen {mutagen.version} — GPLv2 ou ultérieure\n"
        f"Sources : https://github.com/quodlibet/mutagen/releases/tag/release-{mutagen.version}\n\n"
        "Le chargeur PyInstaller est redistribué avec son exception de distribution de programmes.\n"
        "fpcalc/Chromaprint n'est pas redistribué dans ce paquet ; installer séparément cet outil facultatif.\n",
        encoding="utf-8",
    )


def build(output: Path, root: Path = ROOT) -> Path:
    version = project_version(root)
    check_release_tag(version)
    label = platform_label()
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
        "--distpath", str(root / "dist"), "--workpath", str(root / "build" / "pyinstaller"),
        str(root / "musicscope.spec"),
    ], check=True, cwd=root)
    bundle = root / "dist" / "MusicScope"
    shutil.copyfile(root / "LICENSE", bundle / "LICENSE")
    shutil.copyfile(root / "docs" / "distribution.md", bundle / "LISEZ-MOI.md")
    copy_third_party_notices(bundle, root)
    (bundle / "SOURCE.txt").write_text(
        f"MusicScope {version} — GNU AGPLv3\n"
        f"Sources correspondantes : https://github.com/1002-AZ-2001/MusicScope/tree/v{version}\n"
        f"Archive source : https://github.com/1002-AZ-2001/MusicScope/archive/refs/tags/v{version}.tar.gz\n"
        "Python, Qt/PySide6 et Mutagen restent soumis à leurs licences respectives.\n",
        encoding="utf-8",
    )
    smoke_test(bundle)
    archive = archive_bundle(bundle, output, version, label)
    make_debian_package(bundle, output, version, root=root)
    prepare_arch_recipe(archive, root / "build/arch", version, root=root)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release")
    parser.add_argument("--checksums-only", type=Path, metavar="DOSSIER")
    parser.add_argument("--distro-only", type=Path, metavar="BUNDLE", help="Créer le .deb et la recette Arch depuis le bundle déjà testé")
    args = parser.parse_args()
    try:
        if args.checksums_only:
            print(write_checksums(args.checksums_only))
        elif args.distro_only:
            version = project_version()
            check_release_tag(version)
            platform_label()
            smoke_test(args.distro_only)
            print(make_debian_package(args.distro_only, args.output, version))
            print(prepare_arch_recipe(args.output / f"MusicScope-{version}-linux-x86_64.tar.gz",
                                      ROOT / "build/arch", version))
        else:
            print(build(args.output))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Construction MusicScope impossible : {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
