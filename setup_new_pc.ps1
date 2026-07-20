# setup_new_pc.ps1 - bootstrap this repo on a new Windows PC (PowerShell 5.1 compatible)
# Usage: powershell -ExecutionPolicy Bypass -File setup_new_pc.ps1
# NOTE: ASCII only in this file (PS 5.1 reads .ps1 as ANSI without BOM).

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repo
$fail = 0

function Check-Tool($name, $probe) {
    $cmd = Get-Command $probe -ErrorAction SilentlyContinue
    if ($null -eq $cmd) { Write-Host "[MISS] $name  -> install it first"; return $false }
    Write-Host "[ OK ] $name ($($cmd.Source))"
    return $true
}

Write-Host "== 1. tools =="
if (-not (Check-Tool "git" "git")) { $fail = 1 }
if (-not (Check-Tool "GitHub CLI" "gh")) { $fail = 1 }
if (-not (Check-Tool "Python launcher" "py")) { $fail = 1 }
if ($fail -eq 1) { Write-Host "Install missing tools, then re-run."; exit 1 }

Write-Host "== 2. gh auth =="
gh auth status 2>&1 | Select-Object -First 3

Write-Host "== 3. python deps =="
py -3 -m pip install -r requirements.txt --quiet
if ($?) { Write-Host "[ OK ] pip install -r requirements.txt" } else { Write-Host "[FAIL] pip install"; $fail = 1 }

Write-Host "== 4. gitleaks + pre-commit hook =="
$gl = Get-Command gitleaks -ErrorAction SilentlyContinue
if ($null -eq $gl) {
    Write-Host "installing gitleaks via winget..."
    winget install --id Gitleaks.Gitleaks -e --accept-source-agreements --accept-package-agreements --disable-interactivity
}
$hook = @'
#!/bin/sh
# gitleaks secret scan (portable: PATH first, then winget package dir)
GL="$(command -v gitleaks)"
if [ -z "$GL" ]; then
  for c in "$LOCALAPPDATA"/Microsoft/WinGet/Packages/Gitleaks.Gitleaks_*/gitleaks.exe; do
    [ -x "$c" ] && GL="$c" && break
  done
fi
if [ -z "$GL" ]; then echo "gitleaks not found (winget install Gitleaks.Gitleaks)"; exit 1; fi
"$GL" git --pre-commit --staged --redact -v
'@
$hookPath = Join-Path $repo ".git\hooks\pre-commit"
[System.IO.File]::WriteAllText($hookPath, ($hook -replace "`r`n", "`n"))
Write-Host "[ OK ] pre-commit hook written: $hookPath"

Write-Host "== 5. .env skeleton =="
$envPath = Join-Path $repo ".env"
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $repo ".env.example") $envPath
    Write-Host "[ OK ] .env created from .env.example -> fill the 4 keys (see MIGRATION.md sec.3)"
} else {
    Write-Host "[ OK ] .env already exists (not touched)"
}

Write-Host "== 6. masked key check =="
py -3 kakao_sender.py --check

Write-Host ""
Write-Host "Done. Next: fill .env keys, then run:  py -3 main.py --dry-run"
if ($fail -eq 1) { exit 1 } else { exit 0 }
