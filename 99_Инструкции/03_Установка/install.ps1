[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'
$installerPath = Join-Path $PSScriptRoot 'install.py'
$installerArgs = @($installerPath, '--project', $ProjectRoot)
if ($Check) { $installerArgs += '--check' }
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 @installerArgs
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python @installerArgs
} else {
    throw 'Python 3.11+ is required. Install Python, then rerun this installer.'
}
if ($LASTEXITCODE -ne 0) { throw "ai-delegation installer failed (exit $LASTEXITCODE)" }
