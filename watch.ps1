# Keeps Nova alive. Run by the "NovaDiscordBot" scheduled task at logon, but
# it works just as well by hand. Ctrl+C stops it.
#
# The loop is the point: discord.py survives brief network drops on its own,
# but nothing brings it back from a crash or a token hiccup. This does.

Set-Location $PSScriptRoot

$log = Join-Path $PSScriptRoot "watch.log"
$lock = Join-Path $PSScriptRoot "watch.pid"

function Write-Log($message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $log -Value $line -Encoding utf8
}

# Refuse to run twice. A stale pid file from a hard reboot is ignored.
if (Test-Path $lock) {
    $old = Get-Content $lock -ErrorAction SilentlyContinue
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Log "Already supervised by pid $old -- exiting."
        exit 0
    }
}
Set-Content -Path $lock -Value $PID -Encoding utf8

# pythonw runs without a console window; python is the visible fallback.
$py = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\pythonw.exe"
if (-not (Test-Path $py)) { $py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
if (-not $py) { Write-Log "No Python found on PATH -- giving up."; exit 1 }

Write-Log "Supervisor started (pid $PID) using $py"

# Back off after repeated fast failures, so a bad token does not spin the CPU.
$fastFailures = 0

while ($true) {
    $started = Get-Date
    $proc = Start-Process -FilePath $py -ArgumentList "bot.py" `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    Write-Log "Bot started (pid $($proc.Id))"
    $proc.WaitForExit()

    $ranFor = (Get-Date) - $started
    Write-Log ("Bot exited with code {0} after {1:n0}s" -f $proc.ExitCode, $ranFor.TotalSeconds)

    if ($ranFor.TotalSeconds -lt 30) { $fastFailures++ } else { $fastFailures = 0 }

    $wait = [Math]::Min(300, 5 * [Math]::Pow(2, [Math]::Min($fastFailures, 6)))
    Write-Log "Restarting in ${wait}s"
    Start-Sleep -Seconds $wait
}
