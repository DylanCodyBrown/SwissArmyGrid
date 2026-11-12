# Install SwissArmyGrid __VERSION__ (Windows): uv if it is missing, then the grid server as a uv tool, with
# its own Python (uv downloads it; no system Python needed).
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 .\swissarmygrid-__VERSION__-py3-none-any.whl
#
# The repository is private: downloading needs the GitHub CLI signed in (gh auth login), or download the
# wheel from the release page yourself and pass its path.
param([string]$Wheel = "")

$ErrorActionPreference = "Stop"
$Version = "__VERSION__"
$Repo = "DylanCodyBrown/SwissArmyGrid"
$WheelName = "swissarmygrid-$Version-py3-none-any.whl"

function Fail([string]$Message) {
    Write-Host "SwissArmyGrid install: $Message" -ForegroundColor Red
    exit 1
}

# 1. uv: uv's own installer puts it in %USERPROFILE%\.local\bin.
$LocalBin = Join-Path $env:USERPROFILE ".local\bin"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    if (-not (Test-Path (Join-Path $LocalBin "uv.exe"))) {
        Write-Host "Installing uv (the tool that installs SwissArmyGrid and its Python)..."
        try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
        catch { Fail "uv could not be installed; see https://docs.astral.sh/uv/" }
    }
    $env:Path = "$LocalBin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail "uv is installed but not on PATH; open a new terminal and run this again." }

# 2. The wheel: a path given on the command line, or the release's wheel.
$Temp = $null
if ($Wheel) {
    if (-not (Test-Path $Wheel)) { Fail "no such file: $Wheel" }
    $WheelPath = (Resolve-Path $Wheel).Path
} else {
    $Temp = Join-Path ([System.IO.Path]::GetTempPath()) ("sag-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $Temp | Out-Null
    $WheelPath = Join-Path $Temp $WheelName
    $gh = Get-Command gh -ErrorAction SilentlyContinue
    $signedIn = $false
    if ($gh) { gh auth status *> $null; $signedIn = ($LASTEXITCODE -eq 0) }
    if ($signedIn) {
        Write-Host "Downloading $WheelName from the v$Version release..."
        gh release download "v$Version" --repo $Repo --pattern $WheelName --dir $Temp
        if ($LASTEXITCODE -ne 0) { Fail "could not download the release with gh." }
    } else {
        $Url = "https://github.com/$Repo/releases/download/v$Version/$WheelName"
        Write-Host "Downloading $Url ..."
        try { Invoke-WebRequest -Uri $Url -OutFile $WheelPath -UseBasicParsing }
        catch {
            Fail ("could not download the wheel (the repository is private).`n" +
                "  Sign in to the GitHub CLI (gh auth login) and run this again, or download $WheelName from`n" +
                "  https://github.com/$Repo/releases/tag/v$Version and run: install.ps1 <path to the wheel>")
        }
    }
}

# 3. The server, as a uv tool in its own environment.
Write-Host "Installing the SwissArmyGrid server $Version..."
uv tool install --force --python 3.12 "$WheelPath[server]"
if ($LASTEXITCODE -ne 0) { Fail "uv could not install the server." }
uv tool update-shell *> $null
if ($Temp) { Remove-Item -Recurse -Force $Temp -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host "SwissArmyGrid $Version is installed." -ForegroundColor Green
Write-Host "  Start it:              swissarmygrid start"
Write-Host "  The Python client:     uv pip install `"$WheelName`"   (in any project's environment)"
Write-Host "  Then, in Python:       import swissarmygrid as sag; sag.push(df)"
Write-Host "(Open a new terminal first if the swissarmygrid command is not found.)"
