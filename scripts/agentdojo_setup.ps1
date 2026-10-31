[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
$source = Join-Path $repoRoot '.tmp\agentdojo-src'
$environment = Join-Path $repoRoot '.tmp\agentdojo-venv'
$pin = '089ed468cf3ed0322acc66b0211f26d9d90dbf60'
Set-Location -LiteralPath $repoRoot
if (-not (Test-Path -LiteralPath $source)) {
    & git -c http.sslBackend=openssl clone --no-checkout --filter=blob:none 'https://github.com/ethz-spylab/agentdojo.git' $source
    if ($LASTEXITCODE -ne 0) { throw 'External source clone failed' }
}
& git -c http.sslBackend=openssl -C $source checkout --detach $pin
if ($LASTEXITCODE -ne 0) { throw 'Pinned source checkout failed' }
if (-not (Test-Path -LiteralPath $environment)) {
    & uv venv $environment --python (Join-Path $repoRoot '.venv\Scripts\python.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Isolated environment creation failed' }
}
$python = Join-Path $environment 'Scripts\python.exe'
& uv pip sync --python $python (Join-Path $PSScriptRoot 'agentdojo-requirements.lock')
if ($LASTEXITCODE -ne 0) { throw 'Pinned external dependency installation failed' }
& uv pip install --python $python --no-deps $source
if ($LASTEXITCODE -ne 0) { throw 'Pinned AgentDojo installation failed' }
& $python (Join-Path $PSScriptRoot 'agentdojo_calibrate.py')
if ($LASTEXITCODE -ne 0) { throw 'Offline evaluator calibration failed' }
