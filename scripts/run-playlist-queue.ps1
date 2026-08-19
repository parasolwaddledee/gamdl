param(
    [string]$Image = "gamdl-dual:latest",
    [string]$CookiesPath = $env:GAMDL_COOKIES_PATH,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($CookiesPath)) {
    throw "Pass -CookiesPath or set GAMDL_COOKIES_PATH."
}

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$resolvedCookies = (Resolve-Path -LiteralPath $CookiesPath).Path
$downloadsPath = Join-Path $projectRoot "downloads\playlist-queue"
$statePath = Join-Path $projectRoot ".gamdl\playlist-queue"

New-Item -ItemType Directory -Force -Path $downloadsPath | Out-Null
New-Item -ItemType Directory -Force -Path $statePath | Out-Null

$dockerArguments = @(
    "run",
    "--rm",
    "--name", "gamdl-playlist-queue",
    "--volume", "${resolvedCookies}:/config/cookies.txt:ro",
    "--volume", "${downloadsPath}:/downloads",
    "--volume", "${statePath}:/state",
    $Image,
    "gamdl_queue",
    "--cookies-path", "/config/cookies.txt",
    "--output-root", "/downloads",
    "--state-dir", "/state"
)

if ($DryRun) {
    $dockerArguments += "--dry-run"
}

& docker @dockerArguments
exit $LASTEXITCODE
