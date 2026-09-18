param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
& $Python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw 'Python 가상환경 생성 실패' }
& .\.venv\Scripts\python.exe -m pip install 'pyinstaller==6.22.3'
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 설치 실패' }
# Some portable Python distributions need Tcl resources under the build directory.
$pythonBase = & .\.venv\Scripts\python.exe -c 'import sys; print(sys.base_prefix)'
$tclSource = Join-Path $pythonBase 'tcl'
if (Test-Path -LiteralPath $tclSource) {
    New-Item -ItemType Directory -Path '.build-tools\tcl' -Force | Out-Null
    Copy-Item -Path (Join-Path $tclSource '*') -Destination '.build-tools\tcl' -Recurse -Force
    $env:TCL_LIBRARY = Join-Path $PSScriptRoot '.build-tools\tcl\tcl8.6'
    $env:TK_LIBRARY = Join-Path $PSScriptRoot '.build-tools\tcl\tk8.6'
}
& .\.venv\Scripts\python.exe -c 'import tkinter; t=tkinter.Tk(); t.destroy()'
if ($LASTEXITCODE -ne 0) { throw 'Tkinter 리소스를 확인하세요.' }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onedir --windowed --name CodexProjectTransfer app.py
if ($LASTEXITCODE -ne 0) { throw 'EXE 빌드 실패' }
Copy-Item -LiteralPath '.\README.md' -Destination '.\dist\CodexProjectTransfer\README.md'
Compress-Archive -LiteralPath '.\dist\CodexProjectTransfer' -DestinationPath '.\dist\CodexProjectTransfer-Windows.zip' -Force
