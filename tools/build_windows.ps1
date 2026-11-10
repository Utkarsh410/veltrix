# Build the Windows installer, dist\Veltrix-Data-Setup-<version>.exe: the self-contained app
# (its own Python and libraries) packed by Inno Setup. Run on Windows 10 or 11, x64:
#
#   python -m pip install -e ".[ui,all,package]"
#   powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1
#
# Needs Inno Setup 6 (https://jrsoftware.org/isdl.php, or: choco install innosetup).
# The installer is not code-signed: Windows SmartScreen warns on first run ("More info" ->
# "Run anyway") until it is. See docs/packaging.md.
param(
    [string]$Python = "python",
    [string]$BuildDir = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $BuildDir) { $BuildDir = $Root }
$Dist = Join-Path $BuildDir "dist"
$Work = Join-Path $BuildDir "build"

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) { throw "PyInstaller is missing: $Python -m pip install -e "".[ui,all,package]""" }
$Version = & $Python -c "import tomllib; print(tomllib.load(open(r'$Root\pyproject.toml','rb'))['project']['version'])"

$Iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $Iscc) { $Iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $Iscc)) { throw "Inno Setup 6 is missing: https://jrsoftware.org/isdl.php or choco install innosetup" }

& $Python -m PyInstaller --noconfirm --clean --log-level WARN `
    --distpath $Dist --workpath (Join-Path $Work "pyinstaller") (Join-Path $Root "packaging\veltrix_data.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# The installer's licence page and notices, and its "Open with" entry for every file type.
& $Python (Join-Path $Root "tools\collect_licences.py") (Join-Path $Work "licences") | Out-Null
$Extensions = & $Python -c "import sys; sys.path.insert(0, r'$Root\src'); from veltrix_engine.importers import FORMATS; print(' '.join(sorted({e for f in FORMATS for e in f.extensions} | {'.gz'})))"
$Lines = foreach ($e in $Extensions.Split(" ")) {
    "Root: HKA; Subkey: ""Software\Classes\Applications\Veltrix Data.exe\SupportedTypes""; ValueType: string; ValueName: ""$e""; ValueData: """""
    "Root: HKA; Subkey: ""Software\Classes\$e\OpenWithList\Veltrix Data.exe""; Flags: uninsdeletekey"
}
Set-Content -Path (Join-Path $Work "extensions.iss") -Value $Lines -Encoding UTF8

& $Iscc /Q "/DAppVersion=$Version" "/DSourceDir=$Dist\Veltrix Data" "/DBuildDir=$Work" "/DOutputDir=$Dist" `
    (Join-Path $Root "packaging\windows_installer.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$Setup = Join-Path $Dist "Veltrix-Data-Setup-$Version.exe"
"Built: $Dist\Veltrix Data ({0:N0} MB)" -f ((Get-ChildItem "$Dist\Veltrix Data" -Recurse | Measure-Object Length -Sum).Sum / 1MB)
"       $Setup ({0:N0} MB)" -f ((Get-Item $Setup).Length / 1MB)
