#!/usr/bin/env bash
set -euo pipefail
cd /workspace/MusicScope
export UV_CACHE_DIR=/workspace/.cache/uv
uv sync --locked --extra test --extra build
