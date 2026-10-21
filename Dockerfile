# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.13.4 AS uv

FROM python:3.14-slim-trixie

COPY --from=uv /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_DEV=1 \
    PATH="/app/.venv/bin:$PATH"

# OCR tools: Tesseract (English and Japanese) for scans and photos, poppler for
# PDF text layers and page rendering.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Non-root system user the tool runs as.
RUN groupadd --system app && useradd --system --gid app app

WORKDIR /app

# Install locked dependencies first; cached unless pyproject/uv.lock change.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Then copy the project and install it (README.md is the package readme).
COPY . .
RUN uv sync --locked

USER app

# Flags passed to `docker run` append to the entry point, so the image runs
# `paperless-field-extractor --help` by default.
ENTRYPOINT ["paperless-field-extractor"]
CMD ["--help"]
