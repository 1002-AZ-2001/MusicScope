# Validation — MusicScope 0.2.1

Validation du 8 novembre 2026. Les cibles de distribution sont Debian et Arch Linux x86-64 ; Windows est reporté.

## Publication vérifiée sur GitHub

La [release v0.2.1](https://github.com/1002-AZ-2001/MusicScope/releases/tag/v0.2.1) contient le `.deb`, le `.pkg.tar.zst`, l'archive portable et leurs SHA-256. Le [workflow de publication](https://github.com/1002-AZ-2001/MusicScope/actions/runs/42529092684) a réussi : suite de tests, construction du binaire, installation et retrait sous Debian 12, installation et retrait sous Arch, autotests offscreen et X11, puis publication des fichiers. Ces résultats complètent la validation locale détaillée ci-dessous.

Une hypothèse incorrecte du test de certificats avait fait échouer les premiers jobs v0.2.0 : le test exigeait un chemin par défaut Python alors que l'application doit justement retrouver le bundle système lorsqu'il manque. Le test a été corrigé sans désactiver TLS. Les 271 tests passent aussi sans les variables de confiance injectées par le cloud. Le tag correctif v0.2.1 conserve le tag précédent sans réécrire l'historique.

## Moteur et interface

La suite complète a exécuté **271 tests avec succès** dans un conteneur Debian 12 (Python 3.12.16, glibc 2.36, PySide6 6.12.0, Mutagen 1.48.1). Les tests HTTP simulent les services distants. Les tests Qt vérifient les 11 onglets, les sélections et filtres, les décisions, sessions historiques/réexports, Jellyfin, reconnaissance et pochettes simulées, puis un véritable déplacement et sa restauration sur des fichiers synthétiques.

La première version avait également été validée sous Debian 13 avec Python 3.12.14. FLAC, MP3, AAC/M4A, Ogg Vorbis et Opus réels ont été lus. Chromaprint/fpcalc 1.6.2 officiel a été vérifié par son SHA-256 puis exécuté sur un FLAC synthétique de 15 secondes ; son empreinte et son cache fonctionnent.

## Exécutable et paquets

Construction du binaire PyInstaller sous Debian 12, afin de conserver une base glibc 2.36. Son auto-test a réussi hors du dépôt. Démarrage graphique X11 également vérifié avec Xvfb et le backend Qt xcb, en plus du backend offscreen.

Le vrai paquet Debian a été installé, son lanceur /usr/bin/musicscope a réussi les auto-tests offscreen et X11, puis le paquet a été retiré ; /opt/musicscope a disparu comme attendu.

Le paquet Arch a été créé avec makepkg 7.1.0 non-root dans l'image officielle Arch Linux 20261101.0.620205. Sources locales vérifiées par SHA-256, métadonnées .PKGINFO/.BUILDINFO/.MTREE générées et dépendances déclarées. Dans le cloud, les miroirs Arch ont refusé les connexions via le proxy 403 : les dépendances graphiques Arch n'ont pas pu être installées, et l'essai du binaire dans cette image minimale échoue faute de libGL.so.1. Il ne s'agit pas d'un test d'installation Arch réussi. Le workflow GitHub impose une installation normale et des auto-tests offscreen/X11 sur Arch avant toute publication de version.

## Commandes utilisées

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

## Accès et limites

Le proxy cloud refuse l'API GitHub et l'accès réel à MusicBrainz ; les opérations Git natives utilisent l'authentification fournie et sont vérifiées séparément. Les domaines Arch refusés sont fastly.mirror.pkgbuild.com et geo.mirror.pkgbuild.com. Aucune vérification TLS, signature de paquet ou somme de contrôle n'a été désactivée.

Votre serveur Jellyfin, votre médiathèque TrueNAS et les services MusicBrainz/AcoustID/CoverArt réels n'ont pas été validés avec vos accès privés. Le rangement reste limité aux déplacements sur le même volume, sans suppression ni écriture de tags.

La sauvegarde automatique du brouillon cloud n'a pas été confirmée par la plateforme ; les instructions d'installation restent dans le dépôt. La publication Git/GitHub est une opération distincte, à vérifier par ses commits, tags et fichiers effectivement publiés.
