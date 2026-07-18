# deploy.ps1 — Validate JS syntax then upload to VPS
# Usage: powershell -File deploy.ps1
# Or:    powershell -File deploy.ps1 -File server.py   (upload specific file)

param([string]$UploadFile = "")

$VPS  = "Administrator@103.127.146.125"
$DEST = "C:/FarhanFX/"
$SRC  = "e:/Farhan Fx Algo/"

# ── Files to validate + upload ──────────────────────────────────────────────
$files = if ($UploadFile) { @($UploadFile) } else { @("index.html", "server.py", "brain.py") }

Write-Host ""
Write-Host "FarhanFX Deploy Script" -ForegroundColor Cyan
Write-Host ("=" * 40) -ForegroundColor DarkGray

# ── JS syntax check for index.html ──────────────────────────────────────────
function Test-JSSyntax {
    $htmlPath = Join-Path $SRC "index.html"
    if (-not (Test-Path $htmlPath)) { return $true }

    # Check if Node.js is available
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        Write-Host "  [SKIP] Node.js not found — skipping JS syntax check" -ForegroundColor Yellow
        return $true
    }

    # Extract JS from index.html into a temp file
    $html    = Get-Content $htmlPath -Raw -Encoding UTF8
    $matches = [regex]::Matches($html, '(?s)<script(?:\s[^>]*)?>(?!.*?src=)(.*?)</script>')
    $jsBlocks = $matches | ForEach-Object { $_.Groups[1].Value }
    $combined = $jsBlocks -join "`n"

    $tmp = [System.IO.Path]::GetTempFileName() + ".js"
    [System.IO.File]::WriteAllText($tmp, $combined, [System.Text.Encoding]::UTF8)

    $result = & node --check $tmp 2>&1
    Remove-Item $tmp -ErrorAction SilentlyContinue

    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "  SYNTAX ERROR in index.html JS:" -ForegroundColor Red
        Write-Host ("  " + ($result -join "`n  ")) -ForegroundColor Red
        Write-Host ""
        Write-Host "  Upload ABORTED. Fix the error and retry." -ForegroundColor Red
        return $false
    }

    Write-Host "  [OK] JS syntax valid" -ForegroundColor Green
    return $true
}

# ── Run syntax check ─────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Checking syntax..." -ForegroundColor DarkGray
if (-not (Test-JSSyntax)) { exit 1 }

# ── Upload files ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Uploading to VPS ($VPS)..." -ForegroundColor DarkGray
$allOk = $true
foreach ($f in $files) {
    $local = Join-Path $SRC $f
    if (-not (Test-Path $local)) {
        Write-Host "  [SKIP] $f not found" -ForegroundColor Yellow
        continue
    }
    Write-Host "  Uploading $f..." -ForegroundColor Gray -NoNewline
    $out = scp $local "${VPS}:${DEST}${f}" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host " done" -ForegroundColor Green
    } else {
        Write-Host " FAILED" -ForegroundColor Red
        Write-Host ("  " + $out) -ForegroundColor Red
        $allOk = $false
    }
}

# ── Restart server ───────────────────────────────────────────────────────────
if ($allOk -and ("server.py" -in $files -or "brain.py" -in $files -or -not $UploadFile)) {
    Write-Host ""
    Write-Host "Restarting server on VPS..." -ForegroundColor DarkGray
    $cmd = 'Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 2; powershell -File C:\FarhanFX\start_farhan2.ps1'
    $out = ssh $VPS "powershell -Command `"$cmd`"" 2>&1
    Write-Host ("  " + $out) -ForegroundColor $(if ($LASTEXITCODE -eq 0) { "Green" } else { "Red" })
}

Write-Host ""
if ($allOk) {
    Write-Host "Deploy complete!" -ForegroundColor Green
} else {
    Write-Host "Deploy finished with errors." -ForegroundColor Yellow
}
Write-Host ""
