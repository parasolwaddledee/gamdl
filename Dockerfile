ARG RUST_IMAGE=rust:1.89-slim-bookworm
ARG PYTHON_IMAGE=python:3.14-slim-bookworm

FROM ${RUST_IMAGE} AS rust-toolchain

FROM ${PYTHON_IMAGE} AS wheel-builder

ARG GAMDL_VERSION=3.8.5
ARG MATURIN_VERSION=1.14.1

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

COPY pyproject.toml README.md LICENSE ./
COPY gamdl_cn ./gamdl_cn

RUN python -m pip install --no-cache-dir "maturin==${MATURIN_VERSION}" \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels . \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels "gamdl==${GAMDL_VERSION}"

FROM ${PYTHON_IMAGE} AS runtime

ARG GAMDL_VERSION=3.8.5
ARG GAMDL_CN_VERSION=3.8.5+cn

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system gamdl \
    && useradd --system --gid gamdl --create-home gamdl \
    && install --directory --owner gamdl --group gamdl /config /downloads

COPY --from=wheel-builder /wheels /wheels

RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
        "gamdl==${GAMDL_VERSION}" \
        "gamdl_cn==${GAMDL_CN_VERSION}" \
    && rm -rf /wheels

LABEL org.opencontainers.image.source="https://github.com/parasolwaddledee/gamdl" \
      org.opencontainers.image.description="Official gamdl and the gamdl_cn downstream in one image"

USER gamdl
WORKDIR /downloads

VOLUME ["/config", "/downloads"]

CMD ["gamdl", "--help"]
