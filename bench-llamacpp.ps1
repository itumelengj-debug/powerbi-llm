<#
.SYNOPSIS
    Sweeps llama-server configurations for one GGUF and reports load time and tokens/s,
    so you can see whether slow generation is flags, memory pressure or the architecture.

.EXAMPLE
    .\bench-llamacpp.ps1 -Model C:\models\qwen3.8-27b-Q4_K_M.gguf -Server C:\llama\llama-server.exe
.EXAMPLE
    .\bench-llamacpp.ps1 -Model .\model.gguf -SystemFile .\brain.txt -Configs baseline,flash,flash-ctx8k
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Model,
    [string]$Server = 'llama-server.exe',
    [int]$Port = 8099,
    [int]$Context = 32768,
    [string]$SystemFile = '',
    [string]$Prompt = "Measure 'Notional YTD (ZAR)' from [Notional (ZAR)] using 'Date'[Date]. DAX only.",
    [int]$LoadTimeout = 900,
    [string[]]$Configs = @('baseline', 'flash', 'flash-f16kv', 'flash-ctx8k', 'flash-batch'),
    [string]$Extra = '',
    [string]$OutCsv = '.\bench-results.csv',
    [string]$LogDir = '.\bench-logs'
)

$ErrorActionPreference = 'Stop'
$Configs = @($Configs) -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }
if (-not (Test-Path $Model)) { throw "Model not found: $Model" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Base = "http://127.0.0.1:$Port"

# name -> argument list. Every run also gets: -m <model> --host 127.0.0.1 --port <port> --jinja
$Recipes = [ordered]@{
    'baseline'     = @('-ngl', '99', '-c', "$Context", '-ctk', 'q8_0', '-ctv', 'q8_0')
    'flash'        = @('-ngl', '99', '-c', "$Context", '-ctk', 'q8_0', '-ctv', 'q8_0', '-fa', 'on')
    'flash-f16kv'  = @('-ngl', '99', '-c', "$Context", '-fa', 'on')
    'flash-ctx8k'  = @('-ngl', '99', '-c', '8192', '-ctk', 'q8_0', '-ctv', 'q8_0', '-fa', 'on')
    'flash-batch'  = @('-ngl', '99', '-c', "$Context", '-ctk', 'q8_0', '-ctv', 'q8_0', '-fa', 'on', '-b', '2048', '-ub', '512')
    'nodraft'      = @('-ngl', '99', '-c', "$Context", '-ctk', 'q8_0', '-ctv', 'q8_0', '-fa', 'on', '--draft-max', '0')
    'cpu-moe-off'  = @('-ngl', '99', '-c', "$Context", '-ctk', 'q8_0', '-ctv', 'q8_0', '-fa', 'on', '--no-mmap')
}

function Get-GpuMemory {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if (-not $smi) { return $null }
    $line = (& nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits | Select-Object -First 1)
    $p = $line -split ',\s*'
    [pscustomobject]@{ UsedMB = [int]$p[0]; TotalMB = [int]$p[1] }
}

function Wait-Ready([int]$Seconds) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
        try {
            $h = Invoke-RestMethod "$Base/health" -TimeoutSec 5
            if ($h.status -eq 'ok') { return [math]::Round($sw.Elapsed.TotalSeconds, 1) }
        } catch { Start-Sleep -Seconds 2 }
    }
    return $null
}

function Measure-Generation([string]$Text, [int]$MaxTokens) {
    $messages = @()
    if ($SystemFile -and (Test-Path $SystemFile)) {
        $messages += @{ role = 'system'; content = (Get-Content $SystemFile -Raw -Encoding UTF8) }
    }
    $messages += @{ role = 'user'; content = $Text }
    $body = @{ model = 'local'; messages = $messages; stream = $false; max_tokens = $MaxTokens; temperature = 0.3 } |
        ConvertTo-Json -Depth 6
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r = Invoke-RestMethod -Method Post -Uri "$Base/v1/chat/completions" -ContentType 'application/json; charset=utf-8' `
        -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 1800
    $sw.Stop()
    $out = [int]$r.usage.completion_tokens
    [pscustomobject]@{
        Seconds   = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        OutTokens = $out
        TokPerSec = if ($sw.Elapsed.TotalSeconds -gt 0) { [math]::Round($out / $sw.Elapsed.TotalSeconds, 1) } else { 0 }
        PromptTok = [int]$r.usage.prompt_tokens
    }
}

$results = @()
foreach ($name in $Configs) {
    if (-not $Recipes.Contains($name)) { Write-Warning "unknown config '$name' - skipped"; continue }
    $log = Join-Path $LogDir "$name.log"
    $argList = @('-m', $Model, '--host', '127.0.0.1', '--port', "$Port", '--jinja') + $Recipes[$name]
    if ($Extra) { $argList += ($Extra -split '\s+') }
    Write-Host "`n==> $name" -ForegroundColor Cyan
    Write-Host ('    ' + ($Recipes[$name] -join ' '))

    $proc = Start-Process -FilePath $Server -ArgumentList $argList -PassThru -NoNewWindow `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    try {
        $load = Wait-Ready $LoadTimeout
        if ($null -eq $load) {
            Write-Host '    never became ready' -ForegroundColor Red
            $tail = (Get-Content "$log.err" -Tail 4 -ErrorAction SilentlyContinue) -join ' | '
            $results += [pscustomobject]@{ Config = $name; LoadSec = $null; TokPerSec = $null; OutTokens = $null
                VramMB = $null; Note = $tail.Substring(0, [Math]::Min(120, $tail.Length)) }
            continue
        }
        $null = Measure-Generation 'Reply with OK.' 8          # warm-up, not measured
        $mem = Get-GpuMemory
        $run = Measure-Generation $Prompt 400
        Write-Host ("    ready in {0}s, {1} tok/s ({2} tokens), VRAM {3}/{4} MB" -f `
                $load, $run.TokPerSec, $run.OutTokens, $mem.UsedMB, $mem.TotalMB) -ForegroundColor Green
        $results += [pscustomobject]@{ Config = $name; LoadSec = $load; TokPerSec = $run.TokPerSec
            OutTokens = $run.OutTokens; VramMB = $mem.UsedMB; Note = '' }
    } finally {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 5
    }
}

$fmt = '{0,-14} {1,9} {2,9} {3,9} {4,10}  {5}'
Write-Host ''
Write-Host ($fmt -f 'CONFIG', 'LOAD(s)', 'TOK/S', 'TOKENS', 'VRAM(MB)', 'NOTE')
foreach ($r in $results) { Write-Host ($fmt -f $r.Config, $r.LoadSec, $r.TokPerSec, $r.OutTokens, $r.VramMB, $r.Note) }
$results | Export-Csv $OutCsv -NoTypeInformation
Write-Host "`nSaved $OutCsv; server logs in $LogDir" -ForegroundColor Cyan

$ok = @($results | Where-Object { $_.TokPerSec })
if ($ok) {
    $best = $ok | Sort-Object TokPerSec -Descending | Select-Object -First 1
    $baseRun = $ok | Where-Object { $_.Config -eq 'baseline' } | Select-Object -First 1
    Write-Host ("Fastest: {0} at {1} tok/s" -f $best.Config, $best.TokPerSec) -ForegroundColor Green
    if ($baseRun -and $best.TokPerSec -ge 1.5 * $baseRun.TokPerSec) {
        Write-Host "  -> flags were the problem: adopt the $($best.Config) arguments." -ForegroundColor Yellow
    }
    $small = $ok | Where-Object { $_.Config -eq 'flash-ctx8k' } | Select-Object -First 1
    $full = $ok | Where-Object { $_.Config -eq 'flash' } | Select-Object -First 1
    if ($small -and $full -and $small.TokPerSec -ge 1.5 * $full.TokPerSec) {
        Write-Host "  -> memory pressure: the KV cache at $Context is spilling. Lower the context, or set" -ForegroundColor Yellow
        Write-Host "     NVIDIA Control Panel > Manage 3D settings > CUDA - Sysmem Fallback Policy = Prefer No Sysmem Fallback." -ForegroundColor Yellow
    }
    if ($best.TokPerSec -lt 12) {
        Write-Host "  -> still slow on every config: the runtime's kernels for this architecture are the limit." -ForegroundColor Yellow
        Write-Host "     Rebuild the brain on a base your stack runs well (build.ps1 -Base ...) instead of tuning further." -ForegroundColor Yellow
    }
}
