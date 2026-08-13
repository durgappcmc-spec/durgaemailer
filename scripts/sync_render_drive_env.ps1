# Sync Google Drive persistence env vars to Render so prospects survive deploys.
#
# Prerequisites:
#   1) Render CLI installed (this repo installs to %LOCALAPPDATA%\Programs\render-cli)
#   2) API key: https://dashboard.render.com/u/settings#api-keys
#      set RENDER_API_KEY=rnd_...
#   3) Local credentials/bootstrap_token.json (drive.file + spreadsheets scopes)
#   4) RELAY_DRIVE_FOLDER_ID in .env (or pass -FolderId)
#
# Usage:
#   powershell -File scripts/sync_render_drive_env.ps1
#   powershell -File scripts/sync_render_drive_env.ps1 -ServiceName durgaemailer-relay -Deploy
#
param(
    [string]$ServiceName = "durgaemailer-relay",
    [string]$ServiceId = "",
    [string]$FolderId = "",
    [string]$BootstrapTokenPath = "",
    [switch]$Deploy,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$RenderCliDir = Join-Path $env:LOCALAPPDATA "Programs\render-cli"
if (Test-Path (Join-Path $RenderCliDir "render.exe")) {
    $env:Path = "$RenderCliDir;$env:Path"
}

# Prefer API key from .env when the process env is empty
if (-not $env:RENDER_API_KEY) {
    $fromEnv = ""
    $envPath = Join-Path $Root ".env"
    if (Test-Path $envPath) {
        foreach ($line in Get-Content $envPath) {
            if ($line -match "^\s*RENDER_API_KEY\s*=\s*(.+)$") {
                $fromEnv = $Matches[1].Trim().Trim('"').Trim("'")
                break
            }
        }
    }
    if ($fromEnv) { $env:RENDER_API_KEY = $fromEnv }
}

function Read-DotEnvValue([string]$Key) {
    $envPath = Join-Path $Root ".env"
    if (-not (Test-Path $envPath)) { return "" }
    foreach ($line in Get-Content $envPath) {
        if ($line -match "^\s*#" -or $line -notmatch "=") { continue }
        $k, $v = $line.Split("=", 2)
        if ($k.Trim() -eq $Key) { return $v.Trim().Trim('"').Trim("'") }
    }
    return ""
}

if (-not $env:RENDER_API_KEY) {
    Write-Host @"
Missing RENDER_API_KEY.

1. Open https://dashboard.render.com/u/settings#api-keys
2. Create an API key
3. In this terminal:
     `$env:RENDER_API_KEY = 'rnd_...'
4. Re-run:
     powershell -File scripts/sync_render_drive_env.ps1 -Deploy

Optional: also run render login for interactive CLI use.
"@
    exit 1
}

if (-not $BootstrapTokenPath) {
    $BootstrapTokenPath = Join-Path $Root "credentials\bootstrap_token.json"
}
if (-not (Test-Path $BootstrapTokenPath)) {
    throw "Missing bootstrap token at $BootstrapTokenPath. Run: python scripts/bootstrap_google.py"
}
$tokenRaw = (Get-Content $BootstrapTokenPath -Raw).Trim()
# Minify to a single line for Render env
$tokenObj = $tokenRaw | ConvertFrom-Json
$tokenJson = ($tokenObj | ConvertTo-Json -Compress -Depth 20)
if ($tokenJson -notmatch "drive\.file") {
    Write-Warning "Token scopes may be missing drive.file — re-run bootstrap_google.py after enabling Drive API."
}

if (-not $FolderId) {
    $FolderId = Read-DotEnvValue "RELAY_DRIVE_FOLDER_ID"
}
if (-not $FolderId) {
    throw "RELAY_DRIVE_FOLDER_ID missing. Set it in .env or pass -FolderId."
}

$headers = @{
    Authorization = "Bearer $($env:RENDER_API_KEY)"
    Accept        = "application/json"
    "Content-Type" = "application/json"
}

function Invoke-Render([string]$Method, [string]$Path, $Body = $null) {
    $uri = "https://api.render.com/v1$Path"
    if ($null -eq $Body) {
        return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers
    }
    $json = if ($Body -is [string]) { $Body } else { $Body | ConvertTo-Json -Compress -Depth 30 }
    return Invoke-RestMethod -Method $Method -Uri $uri -Headers $headers -Body $json
}

if (-not $ServiceId) {
    Write-Host "Looking up service '$ServiceName'..."
    $services = Invoke-Render GET "/services?limit=50"
    $match = @($services) | Where-Object {
        $_.service.name -eq $ServiceName -or $_.service.id -eq $ServiceName
    } | Select-Object -First 1
    if (-not $match) {
        # API may return bare service objects
        $match = @($services) | Where-Object {
            $_.name -eq $ServiceName -or $_.id -eq $ServiceName
        } | Select-Object -First 1
        if ($match.service) { $ServiceId = $match.service.id }
        elseif ($match.id) { $ServiceId = $match.id }
    } else {
        $ServiceId = $match.service.id
    }
    if (-not $ServiceId) {
        throw "Could not find service named '$ServiceName'. Pass -ServiceId srv-…"
    }
}

Write-Host "Service: $ServiceName ($ServiceId)"
Write-Host "Folder:  $FolderId"
Write-Host "Token:   $($tokenJson.Length) chars (scopes checked locally)"

if ($DryRun) {
    Write-Host "DryRun: would upsert BOOTSTRAP_TOKEN_JSON + RELAY_DRIVE_FOLDER_ID and optionally deploy."
    exit 0
}

Write-Host "Upserting RELAY_DRIVE_FOLDER_ID..."
Invoke-Render PUT "/services/$ServiceId/env-vars/RELAY_DRIVE_FOLDER_ID" @{ value = $FolderId } | Out-Null

Write-Host "Upserting BOOTSTRAP_TOKEN_JSON..."
Invoke-Render PUT "/services/$ServiceId/env-vars/BOOTSTRAP_TOKEN_JSON" @{ value = $tokenJson } | Out-Null

Write-Host "Done. Drive auth + folder pin are on Render."

if ($Deploy) {
    Write-Host "Triggering deploy..."
    $dep = Invoke-Render POST "/services/$ServiceId/deploys" @{ clearCache = "do_not_clear" }
    $depId = $dep.id
    if (-not $depId -and $dep.deploy) { $depId = $dep.deploy.id }
    Write-Host "Deploy started: $depId"
    Write-Host "After it goes live, open Prospects - Drive warning should be gone and contacts restored."
} else {
    Write-Host "Run with -Deploy (or Manual Deploy in the dashboard) to pick up the env vars."
}

# Show CLI availability
if (Get-Command render -ErrorAction SilentlyContinue) {
    Write-Host "Render CLI: $((Get-Command render).Source)"
} else {
    Write-Host "Tip: add $RenderCliDir to PATH for `render` commands."
}
