[CmdletBinding()]
param([string]$ModelPath, [string]$LlamaServerPath, [int]$CtxSize, [int]$GpuLayers, [int]$Port)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
function Config([string]$Key, [string]$Fallback) {
    $value = [Environment]::GetEnvironmentVariable($Key)
    if ($value) { return $value }
    $file = Join-Path $repoRoot '.env'
    if (Test-Path -LiteralPath $file) {
        $line = Get-Content -LiteralPath $file | Where-Object { $_ -match "^\s*$Key\s*=" } | Select-Object -First 1
        if ($line) { return (($line -replace "^\s*$Key\s*=", '').Trim().Trim('"').Trim("'")) }
    }
    return $Fallback
}
if (-not $ModelPath) { $ModelPath = Config 'MODEL_PATH' '' }
if (-not $LlamaServerPath) { $LlamaServerPath = Config 'LLAMA_SERVER_PATH' 'llama-server' }
if (-not $PSBoundParameters.ContainsKey('CtxSize')) { $CtxSize = [int](Config 'LLM_CONTEXT_SIZE' '8192') }
if (-not $PSBoundParameters.ContainsKey('GpuLayers')) { $GpuLayers = [int](Config 'LLAMA_GPU_LAYERS' '99') }
if (-not $PSBoundParameters.ContainsKey('Port')) { $Port = [int](Config 'LLM_PORT' '27362') }
if (-not $ModelPath -or -not (Test-Path -LiteralPath $ModelPath -PathType Leaf)) { throw 'Set MODEL_PATH to the existing GGUF file, or pass -ModelPath.' }
$command = Get-Command $LlamaServerPath -ErrorAction SilentlyContinue
if (-not $command) { throw 'Install a recent CUDA llama.cpp build and set LLAMA_SERVER_PATH to llama-server.exe.' }
$helpText = (& $command.Source --help 2>&1 | Out-String)
function Flag([string[]]$Options) {
    foreach ($option in $Options) {
        if ($helpText -match ('(?m)(?<!\S)' + [regex]::Escape($option) + '(?=[\s,=])')) { return $option }
    }
    throw "Installed llama-server does not advertise required option: $($Options -join ' / '). Check --help or update llama.cpp."
}
$arguments = @((Flag @('--model','-m')), $ModelPath,
              (Flag @('--host')), '127.0.0.1',
              (Flag @('--port')), "$Port",
              (Flag @('--ctx-size','-c')), "$CtxSize",
              (Flag @('--gpu-layers','--n-gpu-layers','-ngl')), "$GpuLayers",
              (Flag @('--alias','-a')), (Config 'LLM_MODEL' 'qwen3.5-4b-local'))
& $command.Source @arguments
exit $LASTEXITCODE
