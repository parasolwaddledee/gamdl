param(
    [string]$Image = "gamdl-dual:latest",
    [string]$CookiesPath = $env:GAMDL_COOKIES_PATH,
    [string]$RcloneConfigPath = (Join-Path $env:APPDATA "rclone\rclone.conf"),
    [string]$RcloneDestination = "music:music",
    [switch]$KeepLocal,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Invoke-GamdlPipeline {
    if ([string]::IsNullOrWhiteSpace($CookiesPath)) {
        throw "Pass -CookiesPath or set GAMDL_COOKIES_PATH."
    }
    if ([string]::IsNullOrWhiteSpace($RcloneDestination)) {
        throw "RcloneDestination cannot be empty."
    }

    $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    $resolvedCookies = (Resolve-Path -LiteralPath $CookiesPath).Path
    $resolvedRcloneConfig = (Resolve-Path -LiteralPath $RcloneConfigPath).Path
    $downloadsPath = Join-Path $projectRoot "downloads\playlist-queue"
    $statePath = Join-Path $projectRoot ".gamdl\playlist-queue"
    $rclone = Get-Command rclone -ErrorAction Stop

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

    & docker @dockerArguments | Out-Host
    $queueExitCode = $LASTEXITCODE

    $targets = @(
        Get-ChildItem -LiteralPath $downloadsPath -Recurse -File |
            Where-Object { $_.Extension.ToLowerInvariant() -in @(".m4a", ".lrc") } |
            ForEach-Object {
                [PSCustomObject]@{
                    Path = $_.FullName
                    Length = $_.Length
                    Md5 = (Get-FileHash -LiteralPath $_.FullName -Algorithm MD5).Hash
                }
            }
    )
    if ($targets.Count -eq 0) {
        Write-Host "R2 upload: no local .m4a or .lrc files found."
        return $queueExitCode
    }

    $copyArguments = @(
        "copy",
        $downloadsPath,
        $RcloneDestination,
        "--config", $resolvedRcloneConfig,
        "--include", "**/*.m4a",
        "--include", "**/*.lrc",
        "--checksum",
        "--stats-one-line",
        "--log-level", "INFO"
    )
    if ($DryRun) {
        $copyArguments += "--dry-run"
    }
    & $rclone.Source @copyArguments | Out-Host
    $copyExitCode = $LASTEXITCODE
    if ($copyExitCode -ne 0) {
        Write-Error "R2 copy failed; local files were retained."
        return $copyExitCode
    }
    if ($DryRun) {
        Write-Host "R2 upload dry-run complete; local files were retained."
        return $queueExitCode
    }

    & $rclone.Source check $downloadsPath $RcloneDestination `
        --config $resolvedRcloneConfig `
        --include "**/*.m4a" `
        --include "**/*.lrc" `
        --one-way `
        --log-level INFO | Out-Host
    $checkExitCode = $LASTEXITCODE
    if ($checkExitCode -ne 0) {
        Write-Error "R2 verification failed; local files were retained."
        return $checkExitCode
    }
    if ($KeepLocal) {
        Write-Host "R2 verification passed; KeepLocal was set, so local files were retained."
        return $queueExitCode
    }

    Add-Type -AssemblyName Microsoft.VisualBasic
    $deletedCount = 0
    foreach ($target in $targets) {
        $resolvedTarget = (Resolve-Path -LiteralPath $target.Path).Path
        if (-not $resolvedTarget.StartsWith(
            $downloadsPath + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing to delete a path outside the queue download directory."
        }
        $currentFile = Get-Item -LiteralPath $resolvedTarget
        $currentMd5 = (Get-FileHash -LiteralPath $resolvedTarget -Algorithm MD5).Hash
        if ($currentFile.Length -ne $target.Length -or $currentMd5 -ne $target.Md5) {
            throw "A local file changed after R2 verification; no changed file was deleted."
        }
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            $resolvedTarget,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
        )
        $deletedCount += 1
    }
    Write-Host "R2 verification passed; moved $deletedCount local file(s) to Recycle Bin."
    return $queueExitCode
}

$mutex = [System.Threading.Mutex]::new($false, "Local\GamdlPlaylistQueueAutomation")
$hasMutex = $false
$exitCode = 1
try {
    try {
        $hasMutex = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $hasMutex = $true
    }
    if (-not $hasMutex) {
        throw "Another gamdl playlist queue run is already active."
    }
    $exitCode = Invoke-GamdlPipeline
}
catch {
    Write-Error $_
    $exitCode = 1
}
finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
exit $exitCode
