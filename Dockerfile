# syntax=docker/dockerfile:1.7
#
# The pdf2epub server image: one image for both the web app and the conversion worker. deploy/compose.yaml
# runs each with its own command and sandbox. Builds for x86-64 and ARM64 (PyTorch is the CPU build there).
#
#   docker build -t pdf2epub .
#
# Everything the converter downloads on first use (Tesseract languages, Docling's models, EPUBCheck, the
# Hebrew word list) is fetched here, because the worker runs without any network access.

ARG PYTHON=3.11

FROM python:${PYTHON}-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PDF2EPUB_HOME=/app \
    HF_HOME=/app/models/hf \
    PATH=/app/.venv/bin:$PATH
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr openjdk-17-jre-headless tini \
    && rm -rf /var/lib/apt/lists/*


FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Dependencies first (a layer that survives code changes), then the app itself, installed non-editable.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable
# Tesseract languages to include. Scanned books in other languages fail with a clear message (no network).
ARG TESSERACT_LANGS=heb,eng,yid,ara,fas,rus,ukr,fra,deu,spa,ita,por,nld,pol
COPY docker/warmup.py /tmp/warmup.py
RUN python /tmp/warmup.py "$TESSERACT_LANGS" && rm -rf /tmp/* /app/models/hf/xet
# Some downloads are private to root (mode 600); the app runs as an unprivileged user and only reads them.
RUN chmod -R a+rX,go-w /app


FROM base
RUN useradd --system --uid 10001 --user-group --home-dir /nonexistent --shell /usr/sbin/nologin pdf2epub \
    && mkdir -p /data/tmp && chown -R 10001:10001 /data
COPY --from=build /app /app
# Read-only at run time: models come from the image, nothing may be downloaded, scratch space is /tmp.
ENV PDF2EPUB_MODE=server \
    PDF2EPUB_DATA=/data \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache
USER 10001:10001
WORKDIR /data
VOLUME /data
EXPOSE 5250
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pdf2epub-web", "--host", "0.0.0.0", "--port", "5250", "--no-browser"]
