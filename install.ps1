# secondbrain-kit installer (Windows). In PowerShell:  .\install.ps1 [--dry-run] [--no-embed] ...
#   .\install.ps1 --doctor | --uninstall
# Requires: Git for Windows (Claude Code runs hooks via Git Bash), Node 20.12+, Claude Code or Codex.
$ErrorActionPreference = 'Stop'
$Kit = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing uv (https://astral.sh/uv)'
    powershell -ExecutionPolicy ByPass -c 'irm https://astral.sh/uv/install.ps1 | iex'
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
$env:PYTHONUTF8 = '1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
uv run --no-project --python 3.12 "$Kit\installer\install.py" @args
exit $LASTEXITCODE
