@echo off
rem Turn the Claude usage overlay back on: re-enable the ClaudeOverlay
rem scheduled task (starts at every logon) and start it right now.

schtasks /change /tn ClaudeOverlay /enable
schtasks /run /tn ClaudeOverlay
echo Overlay enabled and started.
