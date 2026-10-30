# secondbrain-kit one-line installer (Windows PowerShell)
#   irm https://raw.githubusercontent.com/fivetaku/secondbrain-kit/main/bootstrap.ps1 | iex
# Options: set $env:SBKIT_ARGS before running, e.g.  $env:SBKIT_ARGS='--no-codex'
# Re-running updates the kit (fetch + fast-forward; if that is impossible, local commits go to a backup branch and
# changes to git stash before matching origin/main) and reinstalls (idempotent). If fetch fails, the local copy is used as is.
$ErrorActionPreference = 'Stop'
$Repo = if ($env:SBKIT_REPO) { $env:SBKIT_REPO } else { 'https://github.com/fivetaku/secondbrain-kit.git' }
$Dir = if ($env:SBKIT_DIR) { $env:SBKIT_DIR } else { Join-Path $HOME 'secondbrain-kit' }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host 'git is required: winget install Git.Git  (then open a new PowerShell)'
    return
}
if (Test-Path (Join-Path $Dir '.git')) {
    Write-Host "==> Updating kit: $Dir"
    # Remote position before fetching: tells the user's own commits apart from old upstream commits after a history rewrite
    $Old = git -C $Dir rev-parse -q --verify refs/remotes/origin/main
    # No 2> redirection on git: Windows PowerShell 5.1 turns redirected stderr into errors and stops under 'Stop'
    git -C $Dir fetch origin main
    if ($LASTEXITCODE -ne 0) {
        # Fetch itself failed (network etc.): leave the checkout untouched and install what is already here
        Write-Host 'WARNING: could not fetch the latest kit (check network). Local checkout left as is; installing the current copy.'
    } else {
        git -C $Dir merge --ff-only origin/main
        if ($LASTEXITCODE -ne 0) {
            # Fast-forward impossible, e.g. remote history rewritten (2026-10-22 author email cleanup): match origin/main
            Write-Host '==> Cannot fast-forward; resetting to origin/main'
            $Ts = Get-Date -Format yyyyMMdd-HHmmss
            $Not = @('origin/main')
            if ($Old) { $Not += $Old }
            $Local = git -C $Dir rev-list -n 1 HEAD --not @Not
            if ($LASTEXITCODE -ne 0) { throw 'kit update failed (rev-list); nothing was changed' }
            if ($Local) {
                git -C $Dir branch "sbkit-backup-$Ts" HEAD
                if ($LASTEXITCODE -ne 0) { throw 'could not create backup branch; nothing was changed' }
                Write-Host "    Local commits not on origin/main kept in branch sbkit-backup-$Ts (git -C `"$Dir`" log sbkit-backup-$Ts)"
            }
            $Changes = git -C $Dir status --porcelain
            # A failed status would read as 'no changes' and reset --hard without stashing
            if ($LASTEXITCODE -ne 0) { throw 'kit update failed (status); nothing was changed' }
            if ($Changes) {
                git -C $Dir stash push -u -m "sbkit-update-$Ts" | Out-Null
                if ($LASTEXITCODE -ne 0) { throw "could not stash local changes; nothing was reset: $Dir" }
                Write-Host "    Uncommitted changes kept in git stash (git -C `"$Dir`" stash list)"
            }
            git -C $Dir reset --hard origin/main
            if ($LASTEXITCODE -ne 0) { throw 'kit update failed' }
        }
    }
} else {
    Write-Host "==> Cloning kit: $Repo -> $Dir"
    git clone --depth 1 $Repo $Dir
}
$KitArgs = @()
if ($env:SBKIT_ARGS) { $KitArgs = $env:SBKIT_ARGS -split '\s+' | Where-Object { $_ } }
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir 'install.ps1') @KitArgs
