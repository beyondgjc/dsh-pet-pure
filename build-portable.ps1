# dsh-pet-pure portable build: produces dist\dsh-pet-pure-portable\ (+ optional zip).
# Layout = embeddable Python runtime + site-packages (PySide6-Essentials /
# imageio-ffmpeg) + code + webm media (no frameseq cache; generated on first run)
# + launcher scripts. Target machine needs zero dependencies (Windows 10/11);
# extract to a user-writable folder (NOT Program Files).
param([switch]$SkipZip)
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$pyVer = '3.14.2'
$dist = Join-Path $repo 'dist\dsh-pet-pure-portable'
$cache = Join-Path $repo '.build-cache'
$embedZip = Join-Path $cache "python-$pyVer-embed-amd64.zip"
$embedUrl = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-embed-amd64.zip"
$devPython = Join-Path $repo '.venv\Scripts\python.exe'

# 0) clean output
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dist, $cache | Out-Null

# 1) code and docs
Copy-Item (Join-Path $repo 'main.py') $dist -Force
Copy-Item (Join-Path $repo 'pet') (Join-Path $dist 'pet') -Recurse -Force
Copy-Item (Join-Path $repo 'README.md') $dist -Force
Copy-Item (Join-Path $repo 'restart-pet.cmd') $dist -Force
Get-ChildItem $dist -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force

# 2) media (webm sources + manifest; frameseq cache excluded, generated on first run)
New-Item -ItemType Directory -Force -Path (Join-Path $dist 'assets\characters') | Out-Null
Copy-Item 'C:\Users\beyondguo\Documents\Agents\dsh-pet\dsh-pet-indesktop\assets\characters\shenshen' `
  (Join-Path $dist 'assets\characters\shenshen') -Recurse -Force
Remove-Item (Join-Path $dist 'assets\characters\shenshen\frameseq') -Recurse -Force -ErrorAction SilentlyContinue

# 3) embeddable runtime (cached download)
if (-not (Test-Path $embedZip)) {
  Write-Output "downloading $embedUrl"
  Invoke-WebRequest $embedUrl -OutFile $embedZip -UseBasicParsing
}
Expand-Archive $embedZip (Join-Path $dist 'runtime') -Force

# 4) site-packages: dev venv pip --target. PySide6-Essentials covers
#    QtCore/QtGui/QtWidgets; Addons' extra 200MB is unused by this product.
$sp = Join-Path $dist 'runtime\site-packages'
& $devPython -m pip install --target $sp --no-deps --quiet `
  'PySide6-Essentials==6.12.0' 'shiboken6==6.12.0' 'imageio-ffmpeg==0.6.0'
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

# 5) python314._pth: runtime stdlib + site-packages + package root
#    (embeddable isolates all paths; the _pth file is the only sys.path source)
@"
python314.zip
.
site-packages
..
import site
"@ | Set-Content (Join-Path $dist 'runtime\python314._pth') -Encoding ASCII

# 6) launcher scripts (point at the bundled runtime)
@'
@echo off
rem dsh-pet-pure portable launcher: double-click to bring the pet up.
cd /d "%~dp0"
start "" "runtime\pythonw.exe" main.py
'@ | Set-Content (Join-Path $dist 'start-pet.cmd') -Encoding ASCII
@'
@echo off
rem dsh-pet-pure restarter: kills instances, starts fresh.
taskkill /IM pythonw.exe /F >nul 2>&1
cd /d "%~dp0"
start "" "runtime\pythonw.exe" main.py
'@ | Set-Content (Join-Path $dist 'restart-pet.cmd') -Encoding ASCII

# 7) zip
if (-not $SkipZip) {
  Compress-Archive -Path $dist -DestinationPath (Join-Path $repo 'dist\dsh-pet-pure-portable.zip') -Force
}
$size = (Get-ChildItem $dist -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Output ("portable ready: {0} ({1:N0} MB)" -f $dist, $size)
