# PMM MCP installer for Windows (PowerShell 5.1+ or 7+).
# Copies the code to ~\mcp\pmm, stores the settings in ~\.config\mcp\pmm.env (owner-only access)
# and registers the server with Claude Code. Set PMM_URL / PMM_TOKEN beforehand to skip the prompts.
$ErrorActionPreference = 'Stop'
$Src = Split-Path -Parent $MyInvocation.MyCommand.Path

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { throw 'uv not found: install it from https://docs.astral.sh/uv/' }

$Dest = Join-Path $env:USERPROFILE 'mcp\pmm'
$EnvDir = Join-Path $env:USERPROFILE '.config\mcp'
$EnvFile = Join-Path $EnvDir 'pmm.env'
New-Item -ItemType Directory -Force -Path $Dest, $EnvDir | Out-Null
foreach ($f in 'server.py', 'pyproject.toml', 'uv.lock') { Copy-Item (Join-Path $Src $f) $Dest -Force }

if (-not (Test-Path $EnvFile)) {
    $url = $env:PMM_URL
    if (-not $url) { $url = Read-Host 'PMM URL (e.g. https://pmm.example.com)' }
    $token = $env:PMM_TOKEN
    if (-not $token) {
        $secure = Read-Host 'PMM token (Grafana service account, Viewer role)' -AsSecureString
        $token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    }
    if (-not $url -or -not $token) { throw 'URL and token are required.' }
    if ($url -match "['\r\n]" -or $token -match "['\r\n]") { throw "URL and token must not contain quotes or line breaks." }

    # Create the file empty, restrict access to the current user, then write the secrets.
    New-Item -ItemType File -Force -Path $EnvFile | Out-Null
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls $EnvFile /inheritance:r /grant:r "*${sid}:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restrict permissions on the env file.' }
    $content = "PMM_URL='$url'`nPMM_TOKEN='$token'`nPMM_VERIFY=1`n"
    [IO.File]::WriteAllText($EnvFile, $content, (New-Object Text.UTF8Encoding($false)))
}

Push-Location $Dest
try { & $uv.Source sync --quiet } finally { Pop-Location }

$claude = Get-Command claude -ErrorAction SilentlyContinue
$serverArgs = @('run', '--quiet', '--env-file', $EnvFile, '--directory', $Dest, 'server.py')
if ($claude) {
    & claude mcp remove pmm -s user 2>$null | Out-Null
    & claude mcp add pmm -s user -- $uv.Source @serverArgs
    & claude mcp get pmm
} else {
    $json = @{ pmm = @{ command = $uv.Source; args = $serverArgs } } | ConvertTo-Json -Depth 4
    Write-Host 'Claude Code not found. For Claude Desktop, add this under "mcpServers" in claude_desktop_config.json:'
    Write-Host $json
}
