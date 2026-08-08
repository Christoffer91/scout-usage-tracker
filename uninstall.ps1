[CmdletBinding()]
param([switch]$PurgeData)

& (Join-Path $PSScriptRoot "install.ps1") uninstall -PurgeData:$PurgeData
exit $LASTEXITCODE
