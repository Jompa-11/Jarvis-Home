@echo off
rem Double-click to start JARVIS. Run scripts\setup.ps1 once first.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
