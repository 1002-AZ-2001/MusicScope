# MusicScope pour le NAS : travail lourd sans interface (voir docs/truenas.md).
# Lecture seule de la médiathèque montée en /music ; résultat et cache dans /data.
FROM python:3.12-slim-bookworm

# FFmpeg : spectre des FLAC (facultatif mais recommandé).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE* /src/
COPY musicscope /src/musicscope
# Sans Qt : seul mutagen est nécessaire au mode --nas-work (version du verrou uv.lock).
RUN pip install --no-cache-dir "mutagen==1.48.1" \
    && pip install --no-cache-dir --no-deps /src \
    && rm -rf /src \
    && groupadd --gid 568 apps && useradd --uid 568 --gid 568 --no-create-home --shell /usr/sbin/nologin apps

USER 568:568
ENV PYTHONUNBUFFERED=1
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "musicscope"]
CMD ["--nas-work", "/music", "--out", "/data", "--cache", "/data/cache", "--every", "24"]
