# gamdl_cn

[![CI](https://github.com/parasolwaddledee/gamdl/actions/workflows/ci.yml/badge.svg)](https://github.com/parasolwaddledee/gamdl/actions/workflows/ci.yml)
[![Docker](https://github.com/parasolwaddledee/gamdl/actions/workflows/docker.yml/badge.svg)](https://github.com/parasolwaddledee/gamdl/actions/workflows/docker.yml)
[![License](https://img.shields.io/github/license/parasolwaddledee/gamdl)](LICENSE)

`gamdl_cn` is a China-focused downstream of
[glomatico/gamdl](https://github.com/glomatico/gamdl). It preserves the upstream
download, DRM, wrapper, and native media engine while adding localized storefront
and lyrics behavior.

This repository is not an official `gamdl` release. For the full upstream option
reference and usage guide, see the
[official README](https://github.com/glomatico/gamdl#readme).

## Downstream changes

- Defaults to the `cn` storefront and `zh-Hans-CN` metadata language.
- Applies the storefront and optional `?l=` language from each Apple Music URL.
- Sends localized Apple Music and iTunes API requests.
- Tries the syllable-lyrics endpoint before the standard lyrics fallback.
- Selects localized TTML and supports replacement translations.
- Preserves nested, syllable-timed lyric text when producing LRC or SRT output.
- Installs as the separate `gamdl_cn` Python package and CLI, so it can coexist
  with the official `gamdl` package.

The Rust `_ammuxer` engine is unchanged from upstream.

## Install from source

Building requires Python 3.10 or newer, Rust, and Maturin.

```bash
python -m pip install maturin
maturin develop --release
gamdl_cn --version
```

The default configuration path remains `~/.gamdl/config.ini`. When the official
and CN commands are used on the same machine, give them separate configuration
files to avoid sharing language and output settings:

```bash
gamdl --config-path ~/.gamdl/gamdl.ini [OPTIONS] URLS...
gamdl_cn --config-path ~/.gamdl/gamdl_cn.ini [OPTIONS] URLS...
```

## Dual-command Docker image

The Docker image contains both:

- official `gamdl==3.8.5` from PyPI;
- this repository's `gamdl_cn==3.8.5+cn` wheel.

Build locally:

```bash
docker build --tag gamdl-dual .
```

Confirm both commands:

```bash
docker run --rm gamdl-dual gamdl --version
docker run --rm gamdl-dual gamdl_cn --version
```

Run a download with read-only cookies and a writable output mount:

```bash
docker run --rm \
  --volume ./cookies.txt:/config/cookies.txt:ro \
  --volume ./downloads:/downloads \
  gamdl-dual \
  gamdl_cn \
  --cookies-path /config/cookies.txt \
  --config-path /config/gamdl_cn.ini \
  "APPLE_MUSIC_URL"
```

The image runs as an unprivileged `gamdl` user and includes FFmpeg. It does not
include cookies, Widevine device files, credentials, or `N_m3u8DL-RE`; mount or
configure those at runtime as needed.

## Playlist download queues

The image also includes `gamdl_queue`, an idempotent queue processor for these
exact playlists:

- official `gamdl`: `US_Pending`;
- `gamdl_cn`: `CN_Pending`.

For each catalog song, it requires a completed local media file registered in
SQLite and accepted by FFprobe, and only then removes it from `Pending`. A retry
reuses verified downloads. Failed or unsupported items remain in `Pending` for
a later retry or manual review. `US_Downloaded` and `CN_Downloaded` are never
selected as destinations; their track contents are not queried and they are not
modified.

Run a read-only preflight first:

```powershell
.\scripts\run-playlist-queue.ps1 -CookiesPath "C:\path\to\cookies.txt" -DryRun
```

Process both queues:

```powershell
.\scripts\run-playlist-queue.ps1 -CookiesPath "C:\path\to\cookies.txt"
```

Downloads are stored under `downloads/playlist-queue`, while the SQLite state
and the most recent downloader logs stay under the ignored
`.gamdl/playlist-queue` directory. The Cookies file is mounted read-only.

Apple's public Apple Music API does not document removing an individual
playlist track. The final removal therefore uses the endpoint used by the Apple
Music web player and verifies the result immediately. Because that endpoint is
undocumented, Apple may change it without notice; a failed verification leaves
the item in `Pending` for a later retry.

Pushes to `main` and version tags publish the image to:

```text
ghcr.io/parasolwaddledee/gamdl-cn
```

Pull requests build the image without publishing it.

## Updating from upstream

Keep an `upstream` remote pointing to the official repository:

```bash
git remote add upstream https://github.com/glomatico/gamdl.git
git fetch upstream --tags
git merge upstream/main
```

Because this downstream uses the `gamdl_cn` package namespace, new upstream files
under `gamdl/` may need to be moved manually to `gamdl_cn/` while resolving the
merge.

## License and attribution

This downstream retains the upstream MIT license. Copyright and attribution in
the original project remain applicable. See [LICENSE](LICENSE).
