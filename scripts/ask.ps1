# Talk to Aurelia from PowerShell.
#
#   .\scripts\ask.ps1                   interactive: type requests, "exit" to quit
#   .\scripts\ask.ps1 "echo hello"      one request
#
# Keeps one conversation (session) while it runs, and asks you to approve any
# action that needs confirmation.
param(
    [Parameter(Position = 0)] [string] $Text,
    [string] $Url = "http://localhost:8080",
    [string] $User = "owner"
)

$script:SessionId = $null

function Send-Ask([string] $request) {
    $body = @{ text = $request; user = $User }
    if ($script:SessionId) { $body.session_id = $script:SessionId }
    try {
        $r = Invoke-RestMethod -Method Post "$Url/ask" -ContentType 'application/json' `
             -Body ($body | ConvertTo-Json -Depth 5)
    } catch {
        Write-Host "  ! $($_.Exception.Message)" -ForegroundColor Red
        return
    }
    $script:SessionId = $r.session_id
    Write-Host "Aura: $($r.reply)" -ForegroundColor Cyan
    Write-Host ("      [{0}, {1} ms{2}]" -f $r.route, $r.latency_ms,
        $(if ($r.task_ids.Count) { ", tasks: $($r.task_ids.Count)" } else { "" })) -ForegroundColor DarkGray

    foreach ($action in $r.needs_confirmation) {
        $argsText = ($action.args | ConvertTo-Json -Compress)
        $answer = Read-Host "  Confirm $($action.tool) $argsText ? (y/n)"
        $c = Invoke-RestMethod -Method Post "$Url/ask/confirm" -ContentType 'application/json' `
             -Body (@{ action_id = $action.action_id; approve = ($answer -match '^y'); user = $User } | ConvertTo-Json)
        Write-Host "Aura: $($c.reply)" -ForegroundColor Cyan
    }
}

if ($Text) { Send-Ask $Text; return }

Write-Host "Talking to Aurelia at $Url. Type 'exit' to quit." -ForegroundColor DarkGray
while ($true) {
    $line = Read-Host "You"
    if ($line -in @('exit', 'quit')) { break }
    if ($line.Trim()) { Send-Ask $line }
}
