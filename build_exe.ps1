$ErrorActionPreference='Stop'
$sourceRoot=$PSScriptRoot
$buildRoot=Join-Path $sourceRoot 'build'
$releaseRoot=Join-Path $sourceRoot 'dist'
$pythonPrefix=(& python -c 'import sys; print(sys.prefix)').Trim()
$condaDllRoot=Join-Path $pythonPrefix 'Library\bin'
if (Test-Path -LiteralPath $condaDllRoot -PathType Container) {
    $env:PATH="$condaDllRoot;$env:PATH"
}
python -m PyInstaller --clean --noconfirm --onefile --windowed --name CodexGlass --icon (Join-Path $sourceRoot 'assets\codex-glass.ico') --add-data "$(Join-Path $sourceRoot 'assets\codex-glass.png');assets" --add-data "$(Join-Path $sourceRoot 'assets\web\dashboard.html');assets\web" --add-data "$(Join-Path $sourceRoot 'VERSION');." --specpath $buildRoot --workpath $buildRoot --distpath $releaseRoot (Join-Path $sourceRoot 'desktop_widget.py')
if ($LASTEXITCODE -ne 0) {throw 'PyInstaller build failed'}
