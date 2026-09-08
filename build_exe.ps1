$ErrorActionPreference='Stop'
$sourceRoot=$PSScriptRoot
$buildRoot=Join-Path $sourceRoot 'build'
$releaseRoot=Join-Path $sourceRoot 'dist'
python -m PyInstaller --noconfirm --onefile --windowed --name CodexGlass --icon (Join-Path $sourceRoot 'assets\codex-glass.ico') --add-data "$(Join-Path $sourceRoot 'assets\codex-glass.png');assets" --add-data "$(Join-Path $sourceRoot 'VERSION');." --specpath $buildRoot --workpath $buildRoot --distpath $releaseRoot (Join-Path $sourceRoot 'desktop_widget.py')
if ($LASTEXITCODE -ne 0) {throw 'PyInstaller build failed'}
