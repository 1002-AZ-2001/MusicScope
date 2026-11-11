# Feuille de route appliquée

## Version 0.7 — albums en double

| Chantier | Fonction livrée | Limite à connaître |
| --- | --- | --- |
| Détection | Conventions de nommage des dossiers reconnues pour trouver les copies aux tags absents ou différents ; groupes avec niveau de confiance. | Un nom de dossier hors de ces conventions n'est utilisé que comme titre. |
| Recommandation | Copie à garder selon complétude, format, nombre de pistes, tags, MBID et constats ; choix manuel obligatoire si équivalence ou incertitude. | Le format prime sur le nombre de pistes : une édition plus longue en MP3 reste derrière une édition FLAC complète. |
| Quarantaine | Déplacement journalisé vers `_MusicScope_doublons`, restaurable, ignoré par le scan. | Pas de suppression : la quarantaine se vide à la main. Le partage qBittorrent des fichiers déplacés est interrompu. |

## Version 0.6 — éditeur de tags

| Chantier | Fonction livrée | Limite à connaître |
| --- | --- | --- |
| Éditeur | Champs communs d'un album et champs de chaque piste, reprise des propositions MusicBrainz, aperçu avant/après. | Huit champs ; pas de modification groupée de plusieurs albums. |
| Sécurité | Fichier inchangé depuis le scan exigé, valeurs d'origine journalisées avant écriture, relecture de contrôle et rétablissement en cas d'écart. | Pas de copie des fichiers : une coupure pendant l'écriture peut endommager le fichier en cours. |
| Annulation | Journal des tags avec détail et annulation, limitée aux fichiers non modifiés depuis. | — |

## Version 0.5 — interface réorganisée

| Chantier | Fonction livrée | Limite à connaître |
| --- | --- | --- |
| Navigation | Barre latérale en quatre sections avec compteurs à la place de onze onglets ; menus Fichier et Affichage ; Jellyfin et Sources regroupés dans Services. | Les pages et leur contenu sont inchangés. |
| Lisibilité | Thème sombre unifié (tableaux, panneaux de détail, dialogues), chemins relatifs à la racine, statuts de rapprochement colorés, date d'analyse lisible. | Le thème suit la feuille de style de l'application, pas celui du système. |
| Ergonomie | Barre d'état avec progression et annulation, raccourcis clavier, clic droit et double-clic sur l'inventaire, taille de fenêtre mémorisée. | — |

## Version 0.4 — lecture parallèle, cache entretenu et aide

| Chantier | Fonction livrée | Limite à connaître |
| --- | --- | --- |
| Lecture parallèle | Vérification des fichiers et lecture des tags sur 8 threads lorsque la racine est sur un partage réseau ; lecture séquentielle sur disque local. Cache SQLite, table des montages et assemblage restent dans un seul thread : inventaire identique au mode séquentiel. | Gain mesuré sur latence simulée ; votre NAS réel reste à mesurer avec `scripts/benchmark_scan.py --workers 1` puis `--workers 8`. |
| Cache entretenu | Inventaire rechargé au lancement avec sa date ; entrées des fichiers disparus retirées après une analyse complète ; volume du cache affiché. | Chaque analyse vérifie encore la taille et la date de chaque fichier : c'est ce qui garantit qu'une modification de tags est vue. |
| Aide | Icône de fenêtre, menu Aide (documentation, problèmes, dossier du cache), fenêtre « À propos » avec versions et licence. | Les liens ouvrent le navigateur du système. |

## Version 0.3 — charge et cache des points de montage

| Chantier | Fonction livrée | Limite à connaître |
| --- | --- | --- |
| Cache des points de montage | `mountinfo` lu une fois par scan et à chaque nouveau périphérique, décisions mémorisées par dossier ; mêmes résultats que la version 0.2.1, vérifiés par un oracle de 300 tables aléatoires. | Sous Windows, le cache du volume Win32 est testé par simulation seulement. |
| Écritures groupées du cache | Pistes validées par lots de 256, et à la fin d'un scan annulé ou interrompu. | Une autre instance qui lit le cache pendant un scan voit ces entrées avec un léger retard. |
| Test de charge | `scripts/benchmark_scan.py` génère une bibliothèque synthétique et mesure scan à froid, scan à chaud, comparaison, mémoire et lectures de `mountinfo`. | Mesures sur disque local : la latence d'un partage NFS/SMB n'est pas reproduite. |

## Version 0.2

La version 0.2 ajoute les trois chantiers et les idées complémentaires proposés après le premier audit. Les commandes sont accessibles dans les menus de l'application. L'audit reste en lecture seule ; seul le rangement expressément confirmé peut renommer un dossier.

| Chantier | Fonction livrée | Limite à connaître |
| --- | --- | --- |
| Diagnostic Jellyfin | Catalogue paginé par GET, correspondances explicites serveur/poste, fichiers absents, entrées dupliquées et chemins non résolus distincts. | Votre serveur, sa clé API et ses correspondances sont requis ; aucun test sur votre instance privée n'a été effectué. |
| Empreintes et AcoustID | Empreinte Chromaprint locale d'une piste, recherche d'enregistrements à la demande, résultats et liens sourcés. | fpcalc externe et clé d'application AcoustID requis ; un enregistrement ne prouve pas une édition. |
| Pochettes | Consultation des faces avant Cover Art Archive par édition, miniatures, comparaison SHA-256 et liens d'origine. | Des pochettes identiques ne prouvent pas l'identité des albums. |
| Rangement assisté | Prévisualisation Artiste/Album (année), conflits et liens physiques vérifiés, déplacement confirmé et journal de restauration. | Même volume uniquement ; aucun effacement ni écriture de tags. Un album imbriqué non inventorié bloque le déplacement du parent. |
| Distribution | Exécutable PyInstaller pour Debian et Arch Linux, paquets .deb et .pkg.tar.zst, autotest du moteur et de Qt, archives et SHA-256, workflow de publication sur tag. | Linux x86-64 uniquement pour cette publication ; Windows est reporté. |
| Décisions personnelles | « Conserver les deux », « À revoir », « Faux positif », avec note et date dans le cache. | Les décisions deviennent anciennes si leurs preuves changent. Aucune suppression automatique. |
| Estimation d'espace | Volume apparent, identités physiques uniques et borne prudente des copies SHA-256. | Identité inconnue, preuve périmée ou lien externe : estimation indéterminée. |
| Qualité priorisée | Erreurs de lecture, inventaires incomplets, incohérences, numérotation et tags manquants, filtrables par catégorie. | Les constats restent consultatifs. |
| Coût du hachage | Prévisualisation du volume candidat et du volume non encore en cache ; annulation et reprise par cache. | Une taille et une date falsifiées intentionnellement restent une limite du cache. |
| Session réimportable | Export JSON versionné, import historique et correspondances de chemins explicites. | Un import ne devient pas une observation actuelle ; refaire un scan avant les actions sur les fichiers. |
| Alias d'artistes | Recherche MusicBrainz avec identifiants, graphies, alias et liens sourcés. | Les homonymes restent distincts ; aucune fusion automatique ni réécriture du nom. |

## Validation sur votre installation

Essayer d'abord un petit sous-ensemble TrueNAS : multidisque, compilation, import Lidarr avec hardlinks et deux éditions. Vérifier les correspondances serveur/poste Jellyfin avant toute conclusion d'absence.

Les appels HTTP sont simulés dans les tests ; le serveur Jellyfin privé et les services distants réels restent à vérifier avec vos accès. Les paquets Linux sont distincts du code Windows, conservé mais sans distribution binaire pour cette publication.

## Améliorations suivantes possibles

- Distribution signée Windows et installateurs intégrés après essais sur vos postes.
- Mesure sur votre NAS réel avec `scripts/benchmark_scan.py --workers 1` puis `--workers 8`, pour ajuster le nombre de threads.
- Recherche exacte SHA-256 parallèle sur partage réseau, sur le même modèle que le scan.
- Modification d'un champ sur plusieurs albums à la fois, avec le même aperçu et le même journal.
- Écriture des identifiants MusicBrainz proposés.
- Transfert entre volumes avec copie vérifiée et journal multiétape, dans un chantier distinct.
