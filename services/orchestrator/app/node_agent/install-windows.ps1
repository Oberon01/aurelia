# Install Aurelia's node agent on this Windows computer and pair it. In PowerShell (no admin needed):
#
#   & ([scriptblock]::Create((irm https://atlas-ai.<tailnet>.ts.net:8443/node/install-windows.ps1))) -Server https://atlas-ai.<tailnet>.ts.net:8443 -Name "Desk"
#
# Options: -Role workstation|presentation|server (guessed if left out; the devices panel decides).
#
# It installs Python for you if it isn't there (winget, just for you), keeps the agent in
# %LOCALAPPDATA%\Aurelia\node and its key in %APPDATA%\aurelia-node (only your account can read it),
# and starts it when you sign in (a Task Scheduler task, "Aurelia node", running as you, not as
# admin). Approve the code it shows in Aurelia's mailbox. Run it again any time: an already-paired
# computer just gets the latest agent.
#
# Her window: "Aurelia" in the Start menu and on the desktop, or Ctrl+Alt+A: a window of her own (not
# a browser tab), paired once like any display; "send this to the desk" lands in it.
#
# Modes (Start menu, folder "Aurelia"; or ask Aurelia):
#   Gaming mode   - I keep out of the way while you play: nothing heavy, never the GPU. It also turns
#                   on by itself while a game runs (Steam, Epic, GOG, Riot, EA, Ubisoft, Battle.net).
#   Exam mode     - nothing of mine runs on this computer: the agent stops, its autostart is switched
#                   off, and a one-time task brings it back when the time is up (3 hours from the
#                   shortcut). "Aurelia - back to normal" ends it early.
param(
  [Parameter(Mandatory = $true)][string]$Server,
  [string]$Name = $env:COMPUTERNAME,
  [ValidateSet("", "presentation", "workstation", "server")][string]$Role = ""
)
$ErrorActionPreference = "Stop"

function Invoke-Quiet {
  # Runs a program, ignoring what it writes to stderr. Windows PowerShell 5.1 turns a native program's
  # redirected stderr into errors, which "Stop" would make fatal; the exit code ($LASTEXITCODE) is what counts.
  $exe, $rest = $args
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { & $exe @rest 2>$null } finally { $ErrorActionPreference = $old }
}
$Server = $Server.TrimEnd("/")
$Dir = Join-Path $env:LOCALAPPDATA "Aurelia\node"
$Agent = Join-Path $Dir "aurelia_node.py"
$Guard = Join-Path $Dir "aurelia_node_guard.py"
$TaskName = "Aurelia node"

function Get-Python {
  # A real Python 3.9+ (not the Microsoft Store placeholder), as a full path to python.exe.
  foreach ($try in @(@("py", "-3"), @("python"))) {
    if (-not (Get-Command $try[0] -ErrorAction SilentlyContinue)) { continue }
    try {
      $rest = @($try | Select-Object -Skip 1)
      $exe = Invoke-Quiet $try[0] @rest -c "import sys; print(sys.executable if sys.version_info >= (3, 9) else '')"
      if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
    } catch { }
  }
  return $null
}

$Py = Get-Python
if (-not $Py) {
  Write-Host "Installing Python (just for you, through winget)..."
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "Python 3 isn't installed and winget isn't available. Install Python from python.org, then run this again."
  }
  winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Null
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
  $Py = Get-Python
  if (-not $Py) {
    $guess = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $guess) { $Py = $guess } else { throw "Python didn't install. Install it from python.org, then run this again." }
  }
}
$Pyw = Join-Path (Split-Path $Py) "pythonw.exe"
if (-not (Test-Path $Pyw)) { $Pyw = $Py }

Write-Host "Getting the agent from $Server..."
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
foreach ($pair in @(@("agent.py", $Agent), @("guard.py", $Guard))) {
  $new = $pair[1] + ".new"
  Invoke-WebRequest -UseBasicParsing -Uri "$Server/node/$($pair[0])" -OutFile $new
  & $Py -m py_compile $new
  if ($LASTEXITCODE -ne 0) { throw "The download of $($pair[0]) is broken; nothing was changed." }
  Move-Item -Force $new $pair[1]
}

Invoke-Quiet $Py $Agent status | Out-Null
if ($LASTEXITCODE -eq 0) {
  Write-Host "Already paired; the agent is updated."
} else {
  & $Py $Agent pair $Server --name $Name
  if ($LASTEXITCODE -ne 0) { throw "Not paired, so nothing else was set up." }
}
if ($Role) { & $Py $Agent role $Role | Out-Null }

Write-Host "Starting it when you sign in (Task Scheduler: '$TaskName')..."
$me = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute $Pyw -Argument "`"$Guard`" run" -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $me
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Adding the mode shortcuts to the Start menu (folder 'Aurelia')..."
$menu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Aurelia"
New-Item -ItemType Directory -Force -Path $menu | Out-Null
$shell = New-Object -ComObject WScript.Shell
foreach ($s in @(@("Aurelia - exam mode (3 hours)", "mode exam --hours 3"), @("Aurelia - gaming mode", "mode gaming"),
                 @("Aurelia - back to normal", "mode normal"))) {
  $lnk = $shell.CreateShortcut((Join-Path $menu ($s[0] + ".lnk")))
  $lnk.TargetPath = $Pyw
  $lnk.Arguments = "`"$Agent`" $($s[1])"
  $lnk.WorkingDirectory = $Dir
  $lnk.Save()
}

if ($Role -ne "presentation" -and $Role -ne "server") {
  # Her own window on this computer (paired once, like any display): Start menu, desktop, Ctrl+Alt+A.
  foreach ($where in @($menu, [Environment]::GetFolderPath("Desktop"))) {
    $lnk = $shell.CreateShortcut((Join-Path $where "Aurelia.lnk"))
    $lnk.TargetPath = $Pyw
    $lnk.Arguments = "`"$Agent`" app"
    $lnk.WorkingDirectory = $Dir
    $lnk.Description = "Talk to Aurelia"
    if ($where -eq $menu) { $lnk.Hotkey = "CTRL+ALT+A" }
    $lnk.Save()
  }
  Write-Host "Her window: Start menu or desktop 'Aurelia', or Ctrl+Alt+A. The first time, approve it in the mailbox."
}

Start-ScheduledTask -TaskName $TaskName
Write-Host "Aurelia's node agent is running. Its log: $env:APPDATA\aurelia-node\agent.log"
