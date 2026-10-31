[CmdletBinding()]
param(
    [switch]$Execute,
    [switch]$Rehearsal,
    [string]$Cases = 'data/blind-zh',
    [string]$Output = '',
    [string[]]$DomesticApi = @(),
    [ValidateSet('all', 'domestic-api', 'cut-continuation')][string]$RowSet = 'all',
    [string]$ContinuationOf = '',
    [string]$ProveSource = '',
    [string[]]$ProviderEnv = @(),
    [string]$EpisodeCap = '2',
    [ValidateSet('3/2', '1.5')][string]$SafetyFactor = '3/2',
    [string]$Stages = '',
    [Alias('env-file')][string]$EnvFile = '.env'
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $repoRoot
if (-not $Output) { $Output = if ($Rehearsal) { 'evidence/rehearsal' } else { 'evidence/live-zh' } }
$arguments = @('run', '--frozen', 'python', 'scripts/run_live_zh.py', '--cases', $Cases, '--output', $Output, '--episode-cap', $EpisodeCap, '--safety-factor', $SafetyFactor, '--env-file', $EnvFile)
if ($Execute) { $arguments += '--execute' }
if ($Rehearsal) { $arguments += '--rehearsal' }
foreach ($model in $DomesticApi) { $arguments += @('--domestic-api', $model) }
foreach ($binding in $ProviderEnv) { $arguments += @('--provider-env', $binding) }
$arguments += @('--row-set', $RowSet)
if ($ContinuationOf) { $arguments += @('--continuation-of', $ContinuationOf) }
if ($ProveSource) { $arguments += @('--prove-source', $ProveSource) }
if ($Stages) { $arguments += @('--stages', $Stages) }
# No formal call is made unless -Execute is supplied, all gates pass, and an
# complete case comparison fits its pilot-based currency reservation.
& uv @arguments
if ($LASTEXITCODE -ne 0) { throw 'Chinese experiment orchestration failed; retained records contain the reason.' }
