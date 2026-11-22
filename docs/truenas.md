# Travail du NAS sur TrueNAS SCALE

Le travail lourd (lecture des tags, SHA-256 des fichiers de même taille, spectre des FLAC)
peut tourner directement sur le NAS, là où sont les fichiers. Le poste n'a plus qu'à
importer le résultat : son scan suivant ne relit que les fichiers modifiés depuis.

- Le conteneur lit la médiathèque **en lecture seule** et n'y écrit jamais.
- Il n'ouvre **aucun port** et n'a pas besoin de réseau (`network_mode: none`).
- Il écrit un seul fichier, `musicscope-nas.json.gz`, dans un dossier hors de la
  médiathèque. Les chemins y sont relatifs à la racine musicale.

Les décisions (quarantaine, tags, rangement) restent faites sur le poste, dans l'interface.

## 1. Préparer deux datasets

Exemples de chemins, à adapter :

| Rôle | Chemin sur le NAS | Dans le conteneur | Droits pour l'utilisateur `apps` (568) |
| --- | --- | --- | --- |
| Médiathèque | `/mnt/tank/media/Musique` | `/music` (lecture seule) | lecture |
| Résultat et caches | `/mnt/tank/apps/musicscope` | `/data` | lecture et écriture |

Le dataset de résultat ne doit pas être dans la médiathèque : MusicScope le refuse.
Donnez-le à `apps` (Datasets → Permissions, ou `chown -R 568:568 /mnt/tank/apps/musicscope`).
Partagez-le aussi en SMB (lecture suffit) pour que le poste lise le résultat.

## 2. Image

L'image est publiée par le workflow de publication à chaque version :
`ghcr.io/1002-az-2001/musicscope:vX.Y.Z` et `:latest`. Préférez un numéro de version
précis : la mise à jour reste alors une décision.

L'image est publique : TrueNAS la télécharge sans compte GitHub. À partir de la v0.18.1, chaque image porte
une attestation de provenance (Sigstore), vérifiable avec
`gh attestation verify oci://ghcr.io/1002-az-2001/musicscope:vX.Y.Z -R 1002-AZ-2001/MusicScope`.

## 3. Installer l'application

TrueNAS SCALE 24.10 ou plus récent : Apps → Discover Apps → menu ⋮ → **Install via YAML**.
Collez le texte ci-dessous en adaptant les deux chemins de gauche :

```yaml
services:
  musicscope:
    image: ghcr.io/1002-az-2001/musicscope:v0.18.1
    user: "568:568"
    network_mode: none
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    tmpfs: [/tmp]
    restart: unless-stopped
    command: ["--nas-work", "/music", "--out", "/data", "--cache", "/data/cache", "--every", "24"]
    volumes:
      - /mnt/tank/media/Musique:/music:ro
      - /mnt/tank/apps/musicscope:/data
```

TrueNAS peut réordonner le YAML et en retirer les commentaires : c'est normal.

Options de la commande :

| Option | Effet |
| --- | --- |
| `--every 24` | Repasse toutes les 24 h ; sans elle, un seul passage puis arrêt. |
| `--spectrum one` | Spectre d'une piste FLAC par album (par défaut). |
| `--spectrum all` | Spectre de toutes les pistes FLAC : plus long, plus sûr. |
| `--spectrum none` | Pas de spectre. |
| `--no-hash` | Pas de SHA-256 (doublons exacts confirmés plus tard sur le poste). |

Le journal de l'application (Apps → musicscope → Logs) affiche la progression. Les
passages suivants sont rapides : les caches de `/data/cache` évitent de relire les fichiers
inchangés.

## 4. Importer sur le poste

1. Montez la médiathèque en SMB et choisissez comme racine **le même dossier** que
   celui monté en `/music`.
2. Menu Services → **Importer le travail du NAS (tags, SHA-256, spectre)…**
3. Choisissez `musicscope-nas.json.gz` dans le partage du dataset de résultat.

Un fichier n'est repris que si sa taille est identique et sa date de modification égale à
la microseconde près (SMB transmet les dates par pas de 100 ns). Sinon il est relu par le
scan qui suit l'import. Le bilan (pistes, SHA-256 et spectres repris, fichiers modifiés ou
absents) s'affiche dans la barre d'état et le journal.

## Mettre à jour l'image

Le conteneur ne se met pas à jour lui-même (il n'a pas de réseau). Quand l'import signale
« export produit par MusicScope X : mettez à jour l'image du NAS », ou après une nouvelle
version de l'interface :

- **Version précise (recommandé)** : Apps → musicscope → Edit, remplacez `v0.18.1` par le
  nouveau numéro dans la ligne `image:`, puis cliquez sur **Update** en bas de l'écran.
- **Étiquette `:latest`** : TrueNAS surveille les images en amont et affiche « Update
  available » sur la ligne de l'application ; menu ⋮ de l'encadré Application Info →
  **Update**. Ajoutez `pull_policy: always` sous `musicscope:` pour que chaque redémarrage
  récupère la dernière image.

Les caches de `/data/cache` sont conservés : le passage suivant reste rapide.

## Sans TrueNAS

La même commande fonctionne sur toute machine qui voit la médiathèque en local :

```bash
docker run --rm --network none --read-only --tmpfs /tmp -u 568:568 -v /chemin/Musique:/music:ro -v /chemin/musicscope:/data ghcr.io/1002-az-2001/musicscope:v0.18.1
```

Ou, avec MusicScope installé : `musicscope --nas-work /chemin/Musique --out /chemin/musicscope`.
