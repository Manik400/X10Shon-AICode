# Re-runs a batch of applications (ids in data/rerun_batch.json) two at a time through the local
# agent and prints one line per job with its final state. Usage: run from anywhere.
$ErrorActionPreference = "Continue"
$hdr = @{"X-LFA-Client"="dashboard"; "Content-Type"="application/json"}
$base = "http://127.0.0.1:8765"
$batch = Get-Content "D:\MANIK\Projects\Ext\data\rerun_batch.json" -Raw | ConvertFrom-Json
$ids = @($batch.ids)
$pool = @{}
$results = @()
$i = 0
function Report($appId, $taskId) {
    $t = (Invoke-WebRequest -UseBasicParsing -Headers $hdr "$base/api/tasks/$taskId" -TimeoutSec 20 | ConvertFrom-Json).task
    $a = (Invoke-WebRequest -UseBasicParsing -Headers $hdr "$base/api/applications/$appId" -TimeoutSec 20 | ConvertFrom-Json).application
    $u = [string]$a.application_url
    $msg = if ($t.failure_json) { [string]$t.failure_json.message } else { "" }
    $qs = ""
    if ($a.pending_questions_json) { $qs = " | Q: " + (($a.pending_questions_json | ForEach-Object { "$($_.label) [$($_.status)] $($_.reason)" }) -join "; ") }
    $filled = 0
    foreach ($st in $a.fill_plan_json.steps) { $filled += @($st.filled).Count }
    return "$($a.state) | $($u.Substring(0, [Math]::Min(70, $u.Length))) | actions=$($t.action_count) filled=$filled | $($msg.Substring(0, [Math]::Min(140, $msg.Length)))$qs"
}
$started = Get-Date
while ($i -lt $ids.Count -or $pool.Count -gt 0) {
    while ($pool.Count -lt 2 -and $i -lt $ids.Count) {
        $appId = $ids[$i]; $i++
        try {
            $body = @{instruction="Apply to this job"; application_id=$appId} | ConvertTo-Json -Compress
            $t = Invoke-WebRequest -UseBasicParsing -Method Post -Headers $hdr -Body $body "$base/api/tasks" -TimeoutSec 30 | ConvertFrom-Json
            $pool[$appId] = $t.id
            Write-Output "[$(Get-Date -Format HH:mm:ss)] started $($t.id) for $appId"
        } catch {
            $results += "START_FAILED | $appId | $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds 10
    foreach ($appId in @($pool.Keys)) {
        $taskId = $pool[$appId]
        try {
            $s = (Invoke-WebRequest -UseBasicParsing -Headers $hdr "$base/api/tasks/$taskId" -TimeoutSec 20 | ConvertFrom-Json).task
            if ($s.status -notin @("running","queued","pending")) {
                $results += (Report $appId $taskId)
                Write-Output "[$(Get-Date -Format HH:mm:ss)] done $($s.status): $($results[-1])"
                $pool.Remove($appId)
            }
        } catch { Write-Output "poll error $taskId : $($_.Exception.Message)" }
    }
    if (((Get-Date) - $started).TotalMinutes -gt 75) { Write-Output "time limit reached"; break }
}
Write-Output "==== SUMMARY ($($results.Count) jobs) ===="
$results | ForEach-Object { $_ }
