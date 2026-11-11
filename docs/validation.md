# Validation — MusicScope 0.6.0

Validation du 11 novembre 2026. Les cibles de distribution restent Debian et Arch Linux x86-64 ; Windows est reporté.

## Version 0.6.0 — éditeur de tags

L'écriture est testée sur de vrais fichiers des cinq formats : FLAC et MP3 du corpus de démonstration, M4A (AAC), Ogg Vorbis et Opus générés une fois avec ffmpeg et versionnés dans `tests/data` (moins de 5 Ko chacun). Pour chaque format, les huit champs sont écrits puis relus par le lecteur de MusicScope, les autres tags sont conservés, et l'annulation rétablit exactement les tags d'origine. D'autres tests vérifient la conservation d'ID3v2.3, le refus d'un fichier modifié depuis le scan (fichier intact, rien de journalisé), le rétablissement après une relecture différente, le refus d'annuler des tags changés depuis, la suppression d'un champ et son rétablissement, l'arrêt sur annulation entre deux fichiers et la limitation des propositions MusicBrainz aux champs modifiables. Côté interface : écriture, actualisation automatique de l'inventaire, journal, annulation, valeurs mixtes non modifiées, aperçu et refus sur un inventaire historique.

**326 tests** passent sous Python 3.12 et 3.13. Le dialogue d'édition, l'aperçu et le journal ont été contrôlés par captures après une écriture réelle sur les cinq formats.

Non essayé : votre NAS (une écriture réseau interrompue reste un risque), et les fichiers aux tags exotiques (plusieurs balises ID3, APEv2 dans un MP3).

## Version 0.5.0 — interface réorganisée

Les tests d'interface vérifient la barre latérale (sections, onze pages, en-têtes non sélectionnables, synchronisation avec les pages), les compteurs, les menus Fichier et Affichage avec leurs raccourcis, l'affichage relatif des chemins sans modification des données, les actions du clic droit selon la sélection, la barre d'état, la couleur des statuts et la mémorisation de la géométrie avec la racine. **308 tests** passent sous Python 3.12 et 3.13 (Qt `offscreen`). Les écrans Inventaire, Rapprochements et Qualité ont été contrôlés par captures, avec le style Fusion et la palette sombre de l'application.

La disposition n'a pas été essayée sur votre bureau : l'échelle d'affichage et le thème du système peuvent modifier légèrement le rendu.

## Version 0.4.0 — lecture parallèle, cache entretenu et aide

Mesures réalisées dans un conteneur cloud Linux (4 processeurs, ext4 local, Python 3.13.18) avec `scripts/benchmark_scan.py` et des FLAC synthétiques.

**Partage réseau simulé.** 1 ms ajoutée à chaque `stat` et 4 ms à chaque lecture de tags, 3 960 pistes :

| Threads | Scan à froid | Scan à chaud |
| ---: | ---: | ---: |
| 1 | 37,4 s | 14,7 s |
| 8 | 10,6 s | 6,6 s |

**Disque local, 13 200 pistes.** Avec 8 threads forcés, le scan à froid passe de 6,5 s à 14,3 s : le décodage des tags est limité par le GIL de Python. Le mode automatique reste donc séquentiel sur un volume local. En mode automatique, deux mesures de la 0.4.0 donnent 3,3 s et 3,2 s à froid, 1,2 s et 1,0 s à chaud. Sur la même machine, la 0.3.0 donnait entre 3,7 et 4,6 s à froid et entre 1,3 et 1,4 s à chaud : aucune régression, et la variance de la machine dépasse ces écarts.

La latence est simulée : un NAS réel peut se comporter autrement. Pour mesurer, lancer `scripts/benchmark_scan.py --library <dossier vide du partage> --workers 1`, puis la même commande avec `--workers 8`.

Un test compare l'inventaire produit avec 1 et 8 threads, avec des achèvements volontairement désordonnés : il est identique. Autres tests : accès SQLite limité au thread appelant, lectures réellement simultanées, annulation qui conserve en cache toutes les lectures terminées, déconnexion qui préserve l'inventaire précédent, purge limitée aux fichiers disparus de la racine analysée, choix automatique du nombre de threads, menu Aide, fenêtre « À propos » et message de chargement depuis le cache, progression fichier par fichier en mode séquentiel.

**299 tests** passent sous Python 3.12.13 et 3.13.18, Qt en mode `offscreen`. La construction locale PyInstaller réussit : l'auto-test du binaire figé passe et l'icône est embarquée. Le `.deb` 0.4.0 est construit. Le scan JSON du corpus de démonstration donne 8 albums et 16 pistes avec 1 comme avec 4 threads.

## Version 0.3.0 — charge et cache des points de montage

### Publication vérifiée sur GitHub

La [release v0.3.0](https://github.com/1002-AZ-2001/MusicScope/releases/tag/v0.3.0) contient le `.deb`, le `.pkg.tar.zst`, l'archive portable et leurs SHA-256. Le [workflow de publication](https://github.com/1002-AZ-2001/MusicScope/actions/runs/42702837266) a réussi : suite de tests, construction du binaire, installation et retrait sous Debian 12, installation et retrait sous Arch, autotests offscreen et X11, puis publication des fichiers.

### Charge et cache des points de montage

Mesures réalisées avec `scripts/benchmark_scan.py` dans un conteneur cloud Linux (4 processeurs, ext4 local, Python 3.13.18), sur des FLAC synthétiques étiquetés et une copie exacte tous les 10 albums. Même machine et mêmes paramètres avant et après la modification :

| Bibliothèque | Version | Scan à froid | Scan à chaud | Lectures `mountinfo` | Mémoire maximale |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 100 albums, 13 200 pistes | 0.2.1 | 28,5 s | 10,9 s | 13 200 | 168 Mio |
| 1 100 albums, 13 200 pistes | 0.3.0 | 3,7 s | 1,3 s | 1 | 167 Mio |
| 4 400 albums, 52 800 pistes | 0.3.0 | 16,5 s | 7,5 s | 1 | 577 Mio |

Le profilage de la version 0.2.1 attribuait environ 92 % du scan à chaud à la relecture de la table des montages pour chaque fichier, et environ 2,6 s du scan à froid de 3 960 pistes aux validations SQLite individuelles. Sur la bibliothèque de 13 200 pistes, la comparaison des 100 paires candidates donne les mêmes statuts avant et après.

Les inventaires JSON produits par la version 0.2.1 et par la version 0.3.0 sur une même bibliothèque synthétique de 1 381 pistes, avec dossier de disque, sont identiques octet pour octet (hors date de scan), identités physiques comprises. Un test compare aussi la nouvelle décision à l'algorithme 0.2.1 sur 300 tables de montage aléatoires, montages empilés et chemins échappés compris.

La suite complète exécute **283 tests avec succès** sous Python 3.12.13 et 3.13.18, Qt en mode `offscreen`. L'auto-test `--self-test` et le scan JSON du corpus de démonstration réussissent.

Ces chiffres concernent un disque local. Sur un partage NFS ou SMB, la latence des appels `stat` et des lectures de tags domine probablement : mesurer avec le script sur un dossier vide du partage avant d'en tirer une conclusion. Le cache du volume Win32 est vérifié par simulation uniquement.

## Version 0.2.1

### Publication vérifiée sur GitHub

La [release v0.2.1](https://github.com/1002-AZ-2001/MusicScope/releases/tag/v0.2.1) contient le `.deb`, le `.pkg.tar.zst`, l'archive portable et leurs SHA-256. Le [workflow de publication](https://github.com/1002-AZ-2001/MusicScope/actions/runs/42529092684) a réussi : suite de tests, construction du binaire, installation et retrait sous Debian 12, installation et retrait sous Arch, autotests offscreen et X11, puis publication des fichiers. Ces résultats complètent la validation locale détaillée ci-dessous.

Une hypothèse incorrecte du test de certificats avait fait échouer les premiers jobs v0.2.0 : le test exigeait un chemin par défaut Python alors que l'application doit justement retrouver le bundle système lorsqu'il manque. Le test a été corrigé sans désactiver TLS. Les 271 tests passent aussi sans les variables de confiance injectées par le cloud. Le tag correctif v0.2.1 conserve le tag précédent sans réécrire l'historique.

### Moteur et interface

La suite complète a exécuté **271 tests avec succès** dans un conteneur Debian 12 (Python 3.12.16, glibc 2.36, PySide6 6.12.0, Mutagen 1.48.1). Les tests HTTP simulent les services distants. Les tests Qt vérifient les 11 onglets, les sélections et filtres, les décisions, sessions historiques/réexports, Jellyfin, reconnaissance et pochettes simulées, puis un véritable déplacement et sa restauration sur des fichiers synthétiques.

La première version avait également été validée sous Debian 13 avec Python 3.12.14. FLAC, MP3, AAC/M4A, Ogg Vorbis et Opus réels ont été lus. Chromaprint/fpcalc 1.6.2 officiel a été vérifié par son SHA-256 puis exécuté sur un FLAC synthétique de 15 secondes ; son empreinte et son cache fonctionnent.

### Exécutable et paquets

Construction du binaire PyInstaller sous Debian 12, afin de conserver une base glibc 2.36. Son auto-test a réussi hors du dépôt. Démarrage graphique X11 également vérifié avec Xvfb et le backend Qt xcb, en plus du backend offscreen.

Le vrai paquet Debian a été installé, son lanceur /usr/bin/musicscope a réussi les auto-tests offscreen et X11, puis le paquet a été retiré ; /opt/musicscope a disparu comme attendu.

Le paquet Arch a été créé avec makepkg 7.1.0 non-root dans l'image officielle Arch Linux 20261101.0.620205. Sources locales vérifiées par SHA-256, métadonnées .PKGINFO/.BUILDINFO/.MTREE générées et dépendances déclarées. Dans le cloud, les miroirs Arch ont refusé les connexions via le proxy 403 : les dépendances graphiques Arch n'ont pas pu être installées, et l'essai du binaire dans cette image minimale échoue faute de libGL.so.1. Il ne s'agit pas d'un test d'installation Arch réussi. Le workflow GitHub impose une installation normale et des auto-tests offscreen/X11 sur Arch avant toute publication de version.

### Commandes utilisées

Dans le checkout ou sa copie de construction Debian 12 :

```bash
uv sync --locked --extra test --extra build
QT_QPA_PLATFORM=offscreen python -m pytest -q
python -m musicscope --self-test
python scripts/build_release.py
python scripts/build_release.py --distro-only dist/MusicScope
xvfb-run -a env QT_QPA_PLATFORM=xcb dist/MusicScope/MusicScope --self-test
```

Pour le paquet Debian, dans le conteneur de test :

```bash
apt-get install ./musicscope_0.2.0-1_amd64.deb
QT_QPA_PLATFORM=offscreen musicscope --self-test
xvfb-run -a env QT_QPA_PLATFORM=xcb musicscope --self-test
dpkg -r musicscope
```

Pour Arch, dans le conteneur de construction non-root :

```bash
makepkg --noconfirm --nodeps
```

Cette option omet uniquement la vérification des dépendances d'exécution lors de l'empaquetage d'un binaire déjà construit ; elle ne désactive pas les sommes des sources. Le workflow installe ensuite le paquet normalement avec ses dépendances.

### Accès et limites

Le proxy cloud refuse l'API GitHub et l'accès réel à MusicBrainz ; les opérations Git natives utilisent l'authentification fournie et sont vérifiées séparément. Les domaines Arch refusés sont fastly.mirror.pkgbuild.com et geo.mirror.pkgbuild.com. Aucune vérification TLS, signature de paquet ou somme de contrôle n'a été désactivée.

Votre serveur Jellyfin, votre médiathèque TrueNAS et les services MusicBrainz/AcoustID/CoverArt réels n'ont pas été validés avec vos accès privés. Le rangement reste limité aux déplacements sur le même volume, sans suppression ni écriture de tags.

La sauvegarde automatique du brouillon cloud n'a pas été confirmée par la plateforme ; les instructions d'installation restent dans le dépôt. La publication Git/GitHub est une opération distincte, à vérifier par ses commits, tags et fichiers effectivement publiés.
