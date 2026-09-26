# secondbrain-kit one-line installer (Windows PowerShell)
#   irm https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.ps1 | iex
# Options: set $env:SBKIT_ARGS before running, e.g.  $env:SBKIT_ARGS='--no-codex'
# Re-running updates the kit (git pull) and reinstalls (idempotent).
$ErrorActionPreference = 'Stop'
$Repo = if ($env:SBKIT_REPO) { $env:SBKIT_REPO } else { 'https://github.com/fivetaku/secondbrain-kit.git' }
$Dir = if ($env:SBKIT_DIR) { $env:SBKIT_DIR } else { Join-Path $HOME 'secondbrain-kit' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host 'git is required: winget install Git.Git  (then open a new PowerShell)'
    return
}
if (Test-Path (Join-Path $Dir '.git')) {
    Write-Host "==> Updating kit: $Dir"
    git -C $Dir pull --ff-only
} else {
    Write-Host "==> Cloning kit: $Repo -> $Dir"
    git clone --depth 1 $Repo $Dir
}
$KitArgs = @()
if ($env:SBKIT_ARGS) { $KitArgs = $env:SBKIT_ARGS -split '\s+' | Where-Object { $_ } }
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir 'install.ps1') @KitArgs
