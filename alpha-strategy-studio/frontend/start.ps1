$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.\node_modules\react\package.json')) {
    Write-Host 'Installing frontend dependencies...'
    npm install
}
npm run dev
