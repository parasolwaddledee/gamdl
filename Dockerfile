ARG RUST_IMAGE=rust:1.89-slim-bookworm
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG RCLONE_IMAGE=rclone/rclone:1.75.0

FROM ${RCLONE_IMAGE} AS rclone-bin

FROM ${RUST_IMAGE} AS rust-toolchain

FROM ${PYTHON_IMAGE} AS wheel-builder

ARG MATURIN_VERSION=1.14.1
ARG UV_VERSION=0.9.30

ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:${PATH} \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup

RUN apt-get update \
    && apt-get install --yes --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/gamdl_cn

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY gamdl_cn ./gamdl_cn

RUN python -m pip install --no-cache-dir \
        "maturin==${MATURIN_VERSION}" \
        "uv==${UV_VERSION}" \
    && uv export --frozen --no-dev --no-emit-project --no-hashes \
        --output-file /tmp/requirements.txt \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels \
        --requirement /tmp/requirements.txt \
    && python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM ${PYTHON_IMAGE} AS runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GAMDL_COOKIES_PATH=/config/cookies.txt \
    RCLONE_CONFIG=/config/rclone.conf \
    RCLONE_DESTINATION=music:music \
    GAMDL_OUTPUT_ROOT=/downloads \
    GAMDL_STATE_DIR=/state \
    GAMDL_RUN_INTERVAL=1h \
    GAMDL_RUN_IMMEDIATELY=true \
    GAMDL_RUN_ONCE=false \
    GAMDL_KEEP_LOCAL=false \
    GAMDL_DRY_RUN=false \
    GAMDL_QUEUES=us,cn

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system gamdl \
    && useradd --system --gid gamdl --create-home gamdl \
    && install --directory --owner gamdl --group gamdl /config /downloads /state

COPY --from=rclone-bin /usr/local/bin/rclone /usr/local/bin/rclone

COPY --from=wheel-builder /wheels /wheels

RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
        gamdl_cn \
    && rm -rf /wheels

RUN gamdl --version \
    && gamdl_cn --version \
    && gamdl_queue --help > /dev/null \
    && gamdl_pipeline --help > /dev/null \
    && gamdl_service --help > /dev/null

LABEL org.opencontainers.image.source="https://github.com/parasolwaddledee/gamdl" \
      org.opencontainers.image.description="Scheduled Apple Music queue downloader with verified Cloudflare R2 archival"

USER gamdl
WORKDIR /downloads

CMD ["gamdl_service"]
