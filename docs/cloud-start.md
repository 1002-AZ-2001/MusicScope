# Réutiliser l'environnement cloud

Utiliser le checkout existant `/workspace/MusicScope`. Chaque tâche cloud est déjà isolée ; ne pas créer de worktree sauf demande explicite. Lire le README et les éventuelles instructions `AGENTS.md`.

Les dépendances sont conservées dans `.venv`, mais pas les processus. Pour installer ou actualiser les dépendances verrouillées :

```bash
bash /workspace/MusicScope/scripts/cloud-install.sh
cd /workspace/MusicScope
QT_QPA_PLATFORM=offscreen XDG_CACHE_HOME=/workspace/.cache .venv/bin/python -m pytest -q
```

Pour ouvrir l'application dans le bureau partagé disposant d'un affichage graphique :

```bash
cd /workspace/MusicScope
XDG_CACHE_HOME=/workspace/.cache .venv/bin/python -m musicscope --cache /workspace/.cache/musicscope
```

Sans écran, créer un dossier synthétique neuf puis l'analyser :

```bash
.venv/bin/python -m musicscope --demo /tmp/musicscope-demo-nouveau
.venv/bin/python -m musicscope --cache /workspace/.cache/musicscope --scan /tmp/musicscope-demo-nouveau
```

Ne pas placer le cache dans la médiathèque. Aucun serveur web ni aperçu localhost n'est nécessaire. MusicBrainz est facultatif ; autoriser la destination HTTPS `musicbrainz.org` dans les paramètres réseau pour les vérifications réelles. Les tests simulent ce service.

La sauvegarde des champs cloud `install_script` et `start_skill` a été tentée, mais l'outil a retourné une erreur et n'a pas confirmé leur persistance. Ces fichiers conservent les instructions testées dans le dépôt. Ils ne prouvent ni sauvegarde du brouillon cloud ni publication d'un instantané.
