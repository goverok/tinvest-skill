# Установка окружения скилла (Windows, PowerShell). Запуск из папки скилла: powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
$HomeDir = if ($env:TINVEST_HOME) { $env:TINVEST_HOME } else { Join-Path $env:USERPROFILE ".tinvest" }
$SkillDir = Split-Path -Parent $MyInvocation.MyCommand.Path
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
if (-not (Test-Path (Join-Path $HomeDir "venv"))) { python -m venv (Join-Path $HomeDir "venv") }
$Py = Join-Path $HomeDir "venv\Scripts\python.exe"
& $Py -m pip install -q --upgrade pip
& $Py -m pip install -q -r (Join-Path $SkillDir "requirements.txt")
if (-not (Test-Path (Join-Path $HomeDir ".env"))) { Copy-Item (Join-Path $SkillDir ".env.example") (Join-Path $HomeDir ".env") }
if (-not (Test-Path (Join-Path $HomeDir "targets.yaml"))) { Copy-Item (Join-Path $SkillDir "targets.example.yaml") (Join-Path $HomeDir "targets.yaml") }
Write-Host "✓ venv: $HomeDir\venv"
Write-Host "✓ .env: $HomeDir\.env  <- впиши TINVEST_TOKEN (только чтение) и, если нужно, TINVEST_CA_BUNDLE"
Write-Host "✓ targets: $HomeDir\targets.yaml"
Write-Host "Проверка: $Py $SkillDir\scripts\report.py check"
Write-Host "Тесты:    $Py $SkillDir\tests\test_calc.py"
