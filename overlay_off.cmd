@echo off
rem Turn the Claude usage overlay off: stop the running processes and
rem disable the ClaudeOverlay scheduled task so it stays off across logons.
rem Double-click overlay_on.cmd to bring it back.

powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -like '*claude_overlay.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
schtasks /change /tn ClaudeOverlay /disable
echo Overlay stopped and disabled. Run overlay_on.cmd to re-enable.
