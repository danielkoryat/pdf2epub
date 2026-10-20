# syntax=docker/dockerfile:1.7
#
# The voice worker on the server: makes the voices of audiobooks with BlueTTS (on the processor) and Microsoft's
# Edge voices (online). deploy/compose.yaml runs it next to the web app. Chatterbox needs an NVIDIA GPU, so it is
# left out here: it is offered by the local app on a computer that has one.
#
#   docker build -t pdf2epub-voice voice-worker
#
# The BlueTTS models and voices are fetched here, so the worker never downloads code or models at run time.

ARG PYTHON=3.12

FROM python:${PYTHON}-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*


FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /usr/local/bin/uv
# git: BlueTTS is installed from its repository at a fixed commit.
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable
# Making the voice samples downloads the models and every voice, and fails the build if BlueTTS cannot speak.
RUN PDF2EPUB_VOICE_MODELS=/app/models HF_HOME=/app/models/hf PDF2EPUB_VOICE_DATA=/tmp/voice \
    pdf2epub-voice samples --engines blue --out /tmp/samples \
    && rm -rf /tmp/samples /tmp/voice /app/models/hf/xet \
    && chmod -R a+rX,go-w /app


FROM base
RUN useradd --system --uid 10002 --user-group --home-dir /nonexistent --shell /usr/sbin/nologin pdf2epub \
    && mkdir -p /data && chown 10002:10002 /data
COPY --from=build /app /app
ENV PDF2EPUB_VOICE_MODELS=/app/models \
    PDF2EPUB_VOICE_DATA=/data \
    HF_HOME=/app/models/hf \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    NUMBA_CACHE_DIR=/tmp/numba
USER 10002:10002
WORKDIR /data
VOLUME /data
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["pdf2epub-voice", "run"]
