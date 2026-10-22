# Compares Ollama placements on this machine with an identical prompt:
#   A) Intel Iris Xe only (Vulkan), NVIDIA via CUDA left idle
#   B) both GPUs through Vulkan with the model spread across them
# Writes a summary to data\gpu_compare.txt. Leaves Ollama running with the faster setup.
param([string]$Model = "qwen2.5:7b-instruct")
$ErrorActionPreference = "Continue"
$exe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
$env:PATH += ";$env:LOCALAPPDATA\Programs\Ollama"
$out = "D:\MANIK\Projects\Ext\data\gpu_compare.txt"
"GPU comparison $(Get-Date -Format s) model=$Model" | Out-File $out -Encoding utf8

function Start-Cfg([bool]$spread, [bool]$nocuda) {
  $old = @(Get-CimInstance Win32_Process -Filter "Name='ollama.exe'" | Where-Object { $_.CommandLine -match ' serve' } | ForEach-Object { $_.ProcessId })
  # Stop the model runners first: they outlive their server and keep gigabytes of RAM otherwise.
  Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" | Where-Object { $old -contains $_.ParentProcessId } | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }
  $old | ForEach-Object { Stop-Process -Id $_ -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 3
  [Environment]::SetEnvironmentVariable("OLLAMA_HOST", "127.0.0.1:11434", "Process")
  [Environment]::SetEnvironmentVariable("OLLAMA_VULKAN", "1", "Process")
  [Environment]::SetEnvironmentVariable("OLLAMA_IGPU_ENABLE", "1", "Process")
  [Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "-1", "Process")
  [Environment]::SetEnvironmentVariable("OLLAMA_SCHED_SPREAD", $(if ($spread) { "1" } else { $null }), "Process")
  [Environment]::SetEnvironmentVariable("CUDA_VISIBLE_DEVICES", $(if ($nocuda) { "-1" } else { $null }), "Process")
  $log = "D:\MANIK\Projects\Ext\data\ollama_server.log"
  Start-Process -FilePath $exe -ArgumentList "serve" -RedirectStandardError $log -RedirectStandardOutput "$log.out" -WindowStyle Hidden | Out-Null
  for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Seconds 2; if (Select-String -Path $log -Pattern "inference compute" -Quiet) { break } }
  Start-Sleep -Seconds 4
}

function Run([string]$tag) {
  $prompt = "[$tag] " + ("We build storage infrastructure. Requirements: 4+ years Python, Kubernetes, PostgreSQL. " * 30) + " Summarise this job in about 60 words."
  $body = @{ model = $Model; prompt = $prompt; stream = $false; options = @{ num_predict = 80; seed = 1; temperature = 0 } } | ConvertTo-Json
  $r = Invoke-RestMethod -Uri http://127.0.0.1:11434/api/generate -Method Post -Body $body -ContentType "application/json" -TimeoutSec 900
  [pscustomobject]@{ p = $r.prompt_eval_count / ($r.prompt_eval_duration / 1e9); g = $r.eval_count / ($r.eval_duration / 1e9) }
}

$results = @{}
foreach ($cfg in @(@{ n = "A_intel_only"; s = $false; c = $false }, @{ n = "B_both_gpus_spread"; s = $true; c = $true })) {
  Start-Cfg $cfg.s $cfg.c
  $null = Run "warm"
  $a = Run "a"
  $b = Run "b"
  $place = (ollama ps | Select-Object -Skip 1 | Select-Object -First 1) -replace '\s{2,}', ' '
  $nv = (nvidia-smi --query-gpu=memory.used --format=csv,noheader)
  $p = ($a.p + $b.p) / 2; $g = ($a.g + $b.g) / 2
  $results[$cfg.n] = $g + $p / 100
  ("{0,-20} prompt {1,5:N0} tok/s  gen {2,4:N1} tok/s  nvidia={3}  | {4}" -f $cfg.n, $p, $g, $nv, $place) | Out-File $out -Append -Encoding utf8
}
$best = ($results.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 1).Key
"best: $best" | Out-File $out -Append -Encoding utf8
if ($best -eq "A_intel_only") { Start-Cfg $false $false } # B is already the running setup otherwise
"done" | Out-File $out -Append -Encoding utf8
