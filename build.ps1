<#
.SYNOPSIS
    Builds the "powerbi" Ollama model and fits its context window to your GPU.

.DESCRIPTION
    1. Checks the Ollama CLI, server and version.
    2. Sets GPU-friendly Ollama variables (flash attention, q8_0 KV cache, one parallel
       slot, one loaded model, 30 min keep-alive) and restarts Ollama if they changed.
    3. Pulls the base model.
    4. Creates the model at the largest context that stays 100% in VRAM.
    5. Runs a smoke test and prints generation speed.

.EXAMPLE
    .\build.ps1
.EXAMPLE
    .\build.ps1 -Base qwen3.6:27b-coding
.EXAMPLE
    .\build.ps1 -Base qwen3-coder:30b -Name powerbi-fast
#>
[CmdletBinding()]
param(
    [string]$Name = 'powerbi',
    [string]$Base = 'gemma4:26b',
    [ValidateRange(4096, 262144)]
    [int]$MaxContext = 65536,
    [switch]$SkipGpuTuning,
    [switch]$SkipFitTest,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = 'Stop'
$Api = 'http://127.0.0.1:11434/api'
$Here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$ModelfilePath = Join-Path $Here 'Modelfile'
$FullName = if ($Name -match ':') { $Name } else { "$($Name):latest" }
$MinOllama = @{ 'qwen3.8' = '0.32.12' }   # base prefix -> minimum Ollama version

function Write-Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }

function Invoke-Api {
    param([string]$Method = 'Get', [string]$Path, $Body, [int]$TimeoutSec = 30)
    $params = @{ Method = $Method; Uri = "$Api/$Path"; TimeoutSec = $TimeoutSec }
    if ($null -ne $Body) {
        $params.Body = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 8))
        $params.ContentType = 'application/json; charset=utf-8'
    }
    Invoke-RestMethod @params
}

function Test-Server {
    try { $null = Invoke-Api -Path 'version' -TimeoutSec 3; return $true } catch { return $false }
}

function Start-OllamaServer {
    $app = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
    if (Test-Path $app) { Start-Process -FilePath $app }
    else { Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden }
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-Server) { return }
        Start-Sleep -Seconds 1
    }
    throw 'Ollama did not start. Open the Ollama app manually, then re-run build.ps1.'
}

function Stop-OllamaServer {
    Get-Process -Name 'ollama app', 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Test-Server)) { return }
        Start-Sleep -Milliseconds 500
    }
}

function Get-OllamaVersion {
    $text = $null
    try { $text = (Invoke-Api -Path 'version').version } catch { }
    if (-not $text) { $text = (& ollama --version | Out-String) }
    if ($text -match '(\d+)\.(\d+)\.(\d+)') { return [version]"$($Matches[1]).$($Matches[2]).$($Matches[3])" }
    return $null
}

function Get-LoadedModels {
    @((Invoke-Api -Path 'ps').models | Where-Object { $_ })
}

function Clear-LoadedModels {
    foreach ($m in (Get-LoadedModels)) {
        $null = Invoke-Api -Method Post -Path 'generate' -Body @{ model = $m.name; keep_alive = 0 } -TimeoutSec 120
    }
    for ($i = 0; $i -lt 30; $i++) {
        if ((Get-LoadedModels).Count -eq 0) { return }
        Start-Sleep -Milliseconds 500
    }
}

function New-ModelVariant([int]$Ctx) {
    $lines = [IO.File]::ReadAllLines($ModelfilePath, [Text.Encoding]::UTF8)
    $inHeader = $true
    $out = foreach ($line in $lines) {
        if ($inHeader -and $line -match '^SYSTEM\s') { $inHeader = $false }
        if ($inHeader -and $line -match '^FROM\s') { "FROM $Base" }
        elseif ($inHeader -and $line -match '^PARAMETER\s+num_ctx\s') { "PARAMETER num_ctx $Ctx" }
        else { $line }
    }
    $tmp = Join-Path $env:TEMP ('Modelfile.' + ($Name -replace '[^\w.-]', '_'))
    [IO.File]::WriteAllText($tmp, (($out -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
    & ollama create $Name -f $tmp | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "ollama create failed at context $Ctx." }
}

function Get-GpuFit {
    Clear-LoadedModels
    try {
        # An empty prompt loads the model (and allocates its full KV cache) without generating.
        $null = Invoke-Api -Method Post -Path 'generate' -Body @{ model = $Name; keep_alive = '15m' } -TimeoutSec 900
    } catch {
        return [pscustomobject]@{ Ok = $false; Size = 0; Vram = 0; Note = "(load failed: $($_.Exception.Message))" }
    }
    $m = Get-LoadedModels | Where-Object { $_.name -eq $FullName -or $_.model -eq $FullName } | Select-Object -First 1
    if (-not $m) { return [pscustomobject]@{ Ok = $false; Size = 0; Vram = 0; Note = '(not listed by /api/ps)' } }
    [pscustomobject]@{
        Ok   = ([double]$m.size_vram -ge [double]$m.size)
        Size = [double]$m.size
        Vram = [double]$m.size_vram
        Note = ''
    }
}

# ---------------------------------------------------------------------------
Write-Step 'Checking Ollama'
if (-not (Test-Path $ModelfilePath)) { throw "Modelfile not found next to build.ps1 ($ModelfilePath)." }
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    throw 'Ollama is not installed. Install it from https://ollama.com/download and re-run.'
}
if (-not (Test-Server)) { Write-Host 'Starting Ollama...'; Start-OllamaServer }
$ver = Get-OllamaVersion
Write-Host "Ollama $ver"
foreach ($prefix in $MinOllama.Keys) {
    if ($Base -like "$prefix*" -and $ver -and $ver -lt [version]$MinOllama[$prefix]) {
        throw "$Base needs Ollama $($MinOllama[$prefix]) or newer (you have $ver). Update from https://ollama.com/download, or run: .\build.ps1 -Base qwen3.6:27b-coding"
    }
}

if (-not $SkipGpuTuning) {
    Write-Step 'GPU tuning (user environment variables)'
    $gpuEnv = [ordered]@{
        OLLAMA_FLASH_ATTENTION   = '1'
        OLLAMA_KV_CACHE_TYPE     = 'q8_0'
        OLLAMA_NUM_PARALLEL      = '1'
        OLLAMA_MAX_LOADED_MODELS = '1'
        OLLAMA_KEEP_ALIVE        = '30m'
    }
    $changed = @()
    foreach ($k in $gpuEnv.Keys) {
        if ([Environment]::GetEnvironmentVariable($k, 'User') -ne $gpuEnv[$k]) {
            [Environment]::SetEnvironmentVariable($k, $gpuEnv[$k], 'User')
            $changed += $k
        }
        Set-Item -Path "Env:$k" -Value $gpuEnv[$k]
    }
    if ($changed.Count -gt 0) {
        Write-Host "Set: $($changed -join ', '). Restarting Ollama so they take effect..."
        Stop-OllamaServer
        Start-OllamaServer
    } else {
        Write-Host 'Already set.'
    }
}

Write-Step "Pulling base model $Base (first run downloads ~18 GB)"
& ollama pull $Base
if ($LASTEXITCODE -ne 0) { throw "ollama pull $Base failed." }

$ladder = @(131072, 98304, 65536, 49152, 32768, 24576, 16384) | Where-Object { $_ -le $MaxContext }
if (-not $ladder) { $ladder = @($MaxContext) }
$chosen = $null
foreach ($ctx in $ladder) {
    Write-Step "Creating $Name at context $ctx"
    New-ModelVariant $ctx
    if ($SkipFitTest) { $chosen = $ctx; break }
    $fit = Get-GpuFit
    $pct = if ($fit.Size -gt 0) { [math]::Round(100 * $fit.Vram / $fit.Size) } else { 0 }
    Write-Host ('    loaded {0:N1} GB, {1}% on GPU {2}' -f ($fit.Size / 1GB), $pct, $fit.Note)
    if ($fit.Ok) { $chosen = $ctx; break }
}
if (-not $chosen) {
    $chosen = @($ladder)[-1]
    Write-Warning "Even $chosen tokens spills into system RAM (slower). Close other GPU-heavy apps and re-run, or use -Base qwen3-coder:30b."
}

if (-not $SkipSmokeTest) {
    Write-Step 'Smoke test'
    $caps = @((Invoke-Api -Method Post -Path 'show' -Body @{ model = $Name }).capabilities)
    $body = @{
        model    = $Name
        stream   = $false
        messages = @(@{ role = 'user'; content = "Measure 'Notional YTD (ZAR)' from [Notional (ZAR)] using 'Date'[Date]. DAX only." })
    }
    if ($caps -contains 'thinking') { $body.think = $false }
    $r = Invoke-Api -Method Post -Path 'chat' -Body $body -TimeoutSec 900
    Write-Host $r.message.content
    if ($r.eval_duration -gt 0) {
        Write-Host ('    {0:N1} tokens/s' -f ($r.eval_count / ($r.eval_duration / 1e9))) -ForegroundColor Green
    }
    & ollama ps
}

Write-Step 'Done'
Write-Host "Model     $Name   (base $Base, context $chosen)"
Write-Host "Chat      ollama run $Name      (/set nothink = fast, /set think = deep reasoning)"
Write-Host "API       POST $Api/chat   {`"model`": `"$Name`", `"think`": false, ...}"
Write-Host 'Evaluate  .\test.ps1   (smoke)   or   python bench\bench.py   (full benchmark)'
