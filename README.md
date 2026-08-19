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

## Portable scheduled Docker service

The Docker image contains:

- official `gamdl==3.8.5` from PyPI;
- this repository's `gamdl_cn==3.8.5+cn` wheel;
- FFmpeg and the official [rclone 1.75.0](https://github.com/rclone/rclone/releases/tag/v1.75.0)
  binary;
- `gamdl_pipeline` for one verified transaction;
- `gamdl_service` for a continuously scheduled service.

Build locally:

```bash
docker build --tag gamdl-dual .
```

Confirm the installed commands:

```bash
docker run --rm gamdl-dual gamdl --version
docker run --rm gamdl-dual gamdl_cn --version
docker run --rm gamdl-dual gamdl_pipeline --help
docker run --rm gamdl-dual gamdl_service --help
```

The default container command is `gamdl_service`. It runs once immediately and
then waits for `GAMDL_RUN_INTERVAL` after each completed run. The interval accepts
plain seconds or an `s`, `m`, `h`, or `d` suffix, such as `1800`, `30m`, `1h`, or
`1d`.

Copy the parameter template, set the two host file paths, and start the service:

```bash
cp .env.example .env
docker compose up --detach --build
docker compose logs --follow pipeline
```

`GAMDL_COOKIES_FILE` and `RCLONE_CONFIG_FILE` are host-side Compose parameters.
They are mounted read-only at `/config/cookies.txt` and `/config/rclone.conf`.
They must be readable by Docker. Cookies, R2 credentials, Widevine device files,
and other secrets are never copied into the image.

The principal runtime parameters are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GAMDL_RUN_INTERVAL` | `1h` | Delay after each completed run |
| `GAMDL_RUN_IMMEDIATELY` | `true` | Run before the first interval wait |
| `GAMDL_RUN_ONCE` | `false` | Execute one transaction and exit |
| `GAMDL_COOKIES_PATH` | `/config/cookies.txt` | Cookies path inside the container |
| `RCLONE_CONFIG` | `/config/rclone.conf` | rclone config path inside the container |
| `RCLONE_DESTINATION` | `music:music` | Remote and path receiving the files |
| `GAMDL_QUEUES` | `us,cn` | Queues to process (`us`, `cn`, or both) |
| `GAMDL_KEEP_LOCAL` | `false` | Keep verified local files when true |
| `GAMDL_DRY_RUN` | `false` | Read queues and preview copies only |
| `GAMDL_DOWNLOAD_TIMEOUT` | `3600` | Per-track download timeout in seconds |
| `GAMDL_VERIFY_ATTEMPTS` | `6` | Apple playlist removal checks |
| `GAMDL_VERIFY_DELAY` | `3` | Seconds between removal checks |

`/downloads` and `/state` are persistent volumes. `/state` contains the SQLite
download registry, privacy-filtered downloader logs, and `last-run.json`. The
Compose service runs with a read-only root filesystem, a temporary `/tmp`, no
new privileges, and the unprivileged `gamdl` image user.

For a one-shot run without Compose:

```bash
docker run --rm \
  --env GAMDL_RUN_ONCE=true \
  --env RCLONE_DESTINATION=music:music \
  --volume ./cookies.txt:/config/cookies.txt:ro \
  --volume ./rclone.conf:/config/rclone.conf:ro \
  --volume gamdl-downloads:/downloads \
  --volume gamdl-state:/state \
  gamdl-dual
```

The original `gamdl` and `gamdl_cn` commands remain available by overriding the
container command.

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

The portable container pipeline copies all generated `.m4a` and `.lrc` files to
the configured rclone destination, performs a one-way checksum check, and only
then deletes the exact local files whose size and MD5 have not changed. A failed
copy or check keeps the local files for the next run. `GAMDL_DRY_RUN=true`
previews queue and R2 work without deleting anything, and
`GAMDL_KEEP_LOCAL=true` disables local cleanup after a verified upload.

The Windows PowerShell entry point remains available as a compatibility wrapper.
It uses the host rclone configuration and sends verified local files to the
Windows Recycle Bin instead of unlinking them.

For the compatibility wrapper, downloads are staged under
`downloads/playlist-queue`, while SQLite state and the most recent downloader
logs stay under the ignored `.gamdl/playlist-queue` directory. Its runs are
protected by a named mutex. The container service is a single sequential process
and therefore cannot overlap its own scheduled runs.

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
