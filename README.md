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

Building requires Python 3.10 through 3.13, Rust, and Maturin. Python 3.14 is
currently excluded because the DRM dependency chain pins a Protobuf build that
is not compatible with Python 3.14.

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
docker build --tag gamdl-cn:local .
```

Confirm the installed commands:

```bash
docker run --rm gamdl-cn:local gamdl --version
docker run --rm gamdl-cn:local gamdl_cn --version
docker run --rm gamdl-cn:local gamdl_pipeline --help
docker run --rm gamdl-cn:local gamdl_service --help
```

The default container command is `gamdl_service`. It runs once immediately and
then waits for `GAMDL_RUN_INTERVAL` after each completed run. The interval accepts
plain seconds or an `s`, `m`, `h`, or `d` suffix, such as `1800`, `30m`, `1h`, or
`1d`.

The deployment template is designed to be copied into a dedicated Compose root,
for example `D:\Docker\srv` on Windows:

```text
D:\Docker\srv
|-- compose.yml
`-- gamdl
    |-- gamdl.env
    |-- cookies.txt
    |-- rclone.conf
    `-- state
        `-- downloads.sqlite3
```

`downloads.sqlite3` is created automatically for a new deployment. Copy an
existing database into `gamdl/state` before the first start when migrating an
older deployment. Stop the old service before starting the new one; two service
instances must not process and clear the same Apple Music playlists concurrently.

From a repository checkout, create the runtime files and start the GHCR image:

```powershell
New-Item -ItemType Directory -Force D:\Docker\srv\gamdl\state
Copy-Item .\deploy\compose.yaml D:\Docker\srv\compose.yml
Copy-Item .\deploy\gamdl\gamdl.env.example D:\Docker\srv\gamdl\gamdl.env
# Place the real cookies.txt and rclone.conf in D:\Docker\srv\gamdl.
Set-Location D:\Docker\srv
docker compose config
docker compose pull
docker compose up --detach
docker compose logs --follow gamdl
```

Edit `gamdl/gamdl.env` to change runtime behavior. `cookies.txt` and
`rclone.conf` are mounted individually and read-only at
`/config/cookies.txt` and `/config/rclone.conf`. Compose refuses to start when
either source file is absent instead of silently creating an empty directory.
The complete `gamdl` directory is never copied into a locally built image.

The principal runtime parameters and their deployment-template values are:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GAMDL_RUN_INTERVAL` | `6h` | Delay after each completed run |
| `GAMDL_RUN_IMMEDIATELY` | `true` | Run before the first interval wait |
| `RCLONE_DESTINATION` | `music:music` | Remote and path receiving the files |
| `GAMDL_QUEUES` | `us,cn` | Queues to process (`us`, `cn`, or both) |
| `GAMDL_US_PLAYLIST` | `US_Pending` | Exact editable playlist name for official `gamdl` |
| `GAMDL_CN_PLAYLIST` | `CN_Pending` | Exact editable playlist name for `gamdl_cn` |
| `GAMDL_KEEP_LOCAL` | `false` | Keep verified local files when true |
| `GAMDL_DRY_RUN` | `false` | Read queues and preview copies only |
| `GAMDL_DOWNLOAD_TIMEOUT` | `3600` | Per-track download timeout in seconds |
| `GAMDL_VERIFY_ATTEMPTS` | `6` | Apple playlist removal checks |
| `GAMDL_VERIFY_DELAY` | `3` | Seconds between removal checks |

`/downloads` uses the Docker-managed `gamdl-downloads` named volume and is not bound
to a host directory. Keeping this staging area across container replacement
prevents a download from being lost between playlist removal and verified R2
upload. `./gamdl/state` is bound read-write at `/state` and contains the
permanent `downloads.sqlite3` registry, privacy-filtered downloader logs, and
`last-run.json`. Each `media` row stores `id`, `path`, a nullable `source_url`,
`source` (`us` or `cn`), and a nullable UTC ISO 8601 `downloaded_at` timestamp;
`(source, id)` is unique. New downloads receive a timestamp after local media
verification; migrated rows remain null because their original completion time
is unknown. On first startup, legacy `us.sqlite3` and `cn.sqlite3` records are
merged and those files are renamed to `.pre-merge.bak` backups when the host bind
mount permits renames. Otherwise the legacy files remain as unused backups; a
migration marker prevents them from being imported again. The Compose service
runs with a read-only root filesystem, a temporary `/tmp`, all Linux capabilities
dropped, no new privileges, bounded Docker logs, a 65-minute graceful-stop
window, and the unprivileged `gamdl` image user.

For a one-shot run using the same configuration and volumes:

```powershell
docker compose run --rm -e GAMDL_RUN_ONCE=true gamdl
```

Do not set `GAMDL_RUN_ONCE=true` in `gamdl.env`: the long-running service has a
restart policy and would be started repeatedly. Also do not run
`docker compose down --volumes` while staged files may still be awaiting upload,
because that command deletes the `gamdl-downloads` volume.

The original `gamdl` and `gamdl_cn` commands remain available by overriding the
container command.

## Playlist download queues

The image also includes `gamdl_queue`, an idempotent queue processor for two
configurable editable playlists:

- official `gamdl`: `GAMDL_US_PLAYLIST` (default `US_Pending`);
- `gamdl_cn`: `GAMDL_CN_PLAYLIST` (default `CN_Pending`).

For each catalog song, it requires a completed local media file registered in
SQLite and accepted by FFprobe, and only then removes it from `Pending`. A retry
reuses verified downloads. Failed or unsupported items remain in `Pending` for
a later retry or manual review. `US_Downloaded` and `CN_Downloaded` are never
selected as destinations; their track contents are not queried and they are not
modified.

Both queues use the storefront authorized by the supplied Apple Music cookies
and preserve each playlist item's original catalog ID. `CN_Pending` selects the
`gamdl_cn` downloader and `zh-Hans-CN` metadata localization; it does not switch
playback to the China catalog. For example, US account cookies produce a US song
URL with `?l=zh-Hans-CN`, matching Apple Music's localized US links.

Both queues write into the same output root using gamdl's
`Artist/Album/Track` layout. The queue name is not added to the path, so rclone
uploads `Artist/Album/Track.m4a` directly below the configured destination
rather than creating separate `US/` and `CN/` directories. US and CN share one
SQLite registry with a source column, while retaining separate temporary
directories for downloader compatibility.

Run a read-only preflight from the deployment directory:

```powershell
docker compose run --rm `
  -e GAMDL_RUN_ONCE=true `
  -e GAMDL_DRY_RUN=true `
  gamdl
```

The portable container pipeline copies all generated `.m4a` and `.lrc` files to
the configured rclone destination, performs a one-way checksum check, and only
then deletes the exact local files whose size and MD5 have not changed. A failed
copy or check keeps the local files for the next run. `GAMDL_DRY_RUN=true`
previews queue and R2 work without deleting anything, and
`GAMDL_KEEP_LOCAL=true` disables local cleanup after a verified upload.

The container service is a single sequential process and therefore cannot
overlap its own scheduled runs.

Apple's public Apple Music API does not document removing an individual
playlist track. The final removal therefore uses the endpoint used by the Apple
Music web player and verifies the result immediately. Because that endpoint is
undocumented, Apple may change it without notice; a failed verification leaves
the item in `Pending` for a later retry.

Pushes to `main` and semantic version tags such as `3.8.6` publish the image to:

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
