@echo off
setlocal
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%start.ps1" %*
if errorlevel 1 pause
