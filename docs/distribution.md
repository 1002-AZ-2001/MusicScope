# MusicScope pour Debian et Arch Linux

Les exécutables de cette version ciblent **Debian 12 ou plus récent et Arch Linux, en x86-64**. Windows est différé. Les versions GitHub proposent trois formats accompagnés de `SHA256SUMS` :

| Format | Usage |
| --- | --- |
| `musicscope_VERSION-1_amd64.deb` | Installation Debian avec gestion des dépendances et entrée dans le menu. |
| `musicscope-VERSION-1-x86_64.pkg.tar.zst` | Installation Arch Linux avec `pacman` et entrée dans le menu. |
| `MusicScope-VERSION-linux-x86_64.tar.gz` | Version portable sans installation système. |

Python est inclus dans les exécutables. Une session graphique et les bibliothèques graphiques de base de la distribution restent nécessaires pour Qt. Le gel PyInstaller ne remplace ni le noyau, ni les pilotes graphiques, ni toutes les bibliothèques système.

## Debian

Télécharger le `.deb` de la version, puis depuis son dossier :

```bash
sudo apt install ./musicscope_VERSION-1_amd64.deb
musicscope
QT_QPA_PLATFORM=offscreen musicscope --self-test
```

L'application est installée sous `/opt/musicscope`, avec un lanceur `/usr/bin/musicscope`, une icône et une entrée de menu. Ses caches restent dans votre dossier utilisateur. Pour retirer l'application : `sudo apt remove musicscope` ; les caches et médiathèques personnels ne sont pas supprimés.

La dépendance `libc6` du paquet reflète la version de glibc de la machine de construction. Le workflow publié utilise Ubuntu 22.04, puis installe et teste le paquet sous Debian 12. Un paquet construit localement sur une distribution plus récente peut donc exiger une glibc plus récente : consulter `dpkg-deb -f FICHIER.deb Depends` avant de le redistribuer.

## Arch Linux

Sur un système Arch à jour, télécharger le paquet puis :

```bash
sudo pacman -U ./musicscope-VERSION-1-x86_64.pkg.tar.zst
musicscope
QT_QPA_PLATFORM=offscreen musicscope --self-test
```

Les dépendances listées dans le paquet doivent être installées depuis les dépôts Arch si elles ne sont pas déjà présentes. L'application dispose des mêmes chemins et de la même entrée de menu que le paquet Debian. Pour la retirer : `sudo pacman -R musicscope`.

Le paquet est construit par `makepkg`, exécuté sans privilèges root dans une image Arch officielle. Le workflow vérifie son installation, son auto-test et sa désinstallation dans un second conteneur Arch. Le paquet de version GitHub n'est pas signé avec une clé de mainteneur Arch ; comparer son SHA-256 à celui de la version téléchargée.

## Version portable

```bash
tar -xzf MusicScope-VERSION-linux-x86_64.tar.gz
./MusicScope/MusicScope
QT_QPA_PLATFORM=offscreen ./MusicScope/MusicScope --self-test
sha256sum -c SHA256SUMS --ignore-missing
```

Conserver le dossier `MusicScope` entier : l'exécutable dépend du sous-dossier `_internal`. La version portable est soumise à la même compatibilité glibc que la machine de construction. Les paquets officiels visent Debian 12 et Arch Linux ; aucune compatibilité avec toutes les distributions Linux n'est affirmée.

## Empreintes audio et services facultatifs

Chromaprint utilise `fpcalc`, une dépendance externe : `sudo apt install libchromaprint-tools` sous Debian ou `sudo pacman -S chromaprint` sous Arch Linux. MusicScope utilise cet outil depuis `PATH` ou le chemin choisi dans l'interface. Une recherche AcoustID exige votre clé applicative et une connexion réseau. Jellyfin exige votre serveur et sa clé API. MusicBrainz et les pochettes utilisent leurs services publics. Aucun identifiant personnel n'est inclus dans les paquets.

## Construire depuis les sources

Pour obtenir une compatibilité Debian 12, construire dans Debian 12 ou une base glibc plus ancienne compatible. Depuis la racine du dépôt, avec Python 3.12, `uv` et `dpkg-deb` :

```bash
uv sync --locked --extra test --extra build
uv run --no-sync python -m pytest -q
uv run --no-sync python scripts/build_release.py
```

Cette commande produit le bundle `dist/MusicScope`, vérifie `--help` et `--self-test` hors du dépôt, puis génère le `.tar.gz`, le `.deb` et la recette `build/arch/PKGBUILD`. Les fichiers audio synthétiques et les licences sont inclus. Pour reconstruire les paquets depuis un bundle déjà testé sans regeler Python : `python scripts/build_release.py --distro-only dist/MusicScope` ; son archive portable doit déjà exister dans `release/`.

Sous Arch Linux, copier le dossier `build/arch` généré puis y lancer `makepkg --noconfirm --nodeps` en utilisateur ordinaire. Le PKGBUILD vérifie les SHA-256 de ses sources locales et conserve les bibliothèques gelées sans les recompiler ni les dépouiller. Copier le `.pkg.tar.zst` dans `release/`, puis recalculer `python scripts/build_release.py --checksums-only release`.

Les dépendances Python sont verrouillées ; les archives ne sont pas annoncées identiques bit à bit entre machines. Les déploiements ne reposent sur aucun script de modification de médiathèque à l'installation.

## Publication et licences

Un tag correspondant exactement à la version applicative, par exemple `v0.2.0`, déclenche le workflow Linux. La publication GitHub attend la réussite des tests du moteur et de Qt, du binaire gelé, ainsi que de l'installation/auto-test/retrait sous Debian 12 et Arch Linux. Une exécution manuelle construit les artefacts et ne publie aucune version. La présence du workflow ne prouve pas son exécution : consulter le résultat associé à la version téléchargée.

MusicScope est distribué sous GNU AGPLv3. `LICENSE`, `THIRD_PARTY.txt`, le dossier `licenses` et les URL des sources correspondantes sont inclus. Qt/PySide6 est lié dynamiquement et reste remplaçable dans `_internal`. Les sources complètes du projet sont accessibles via les archives automatiques du tag GitHub. Les essais sur votre bureau et votre NAS complètent les auto-tests sans écran.
