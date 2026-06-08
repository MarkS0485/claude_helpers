@echo off
rem Turn the Claude usage overlay back on. First repair the ClaudeOverlay
rem scheduled task so it points at THIS folder's claude_overlay.py (the overlay
rem survives the repo being moved to another drive/path), then enable it for
rem logon and start it right now. Double-click overlay_off.cmd to stop it.

setlocal
set "OVL_DIR=%~dp0"
if "%OVL_DIR:~-1%"=="\" set "OVL_DIR=%OVL_DIR:~0,-1%"
set "OVL_SCRIPT=%OVL_DIR%\claude_overlay.py"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$exe=(Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source; if(-not $exe){$exe='pythonw.exe'}; $a=New-ScheduledTaskAction -Execute $exe -Argument ($env:OVL_SCRIPT + ' --edge 4') -WorkingDirectory $env:OVL_DIR; try { Set-ScheduledTask -TaskName ClaudeOverlay -Action $a -ErrorAction Stop | Out-Null; Write-Output ('repaired ClaudeOverlay task -> ' + $env:OVL_SCRIPT) } catch { Write-Output ('WARN: could not update task: ' + $_.Exception.Message) }"

schtasks /change /tn ClaudeOverlay /enable >nul
schtasks /run /tn ClaudeOverlay
echo Overlay enabled and started.
endlocal
