@echo off
REM QuantDesk launcher: start backend (uvicorn on 127.0.0.1:8787), then open the desktop shell.
REM Encoding rules for this file (both caused real failures on this machine):
REM  * ASCII only + CRLF endings. cmd.exe reads a .bat by byte offset, so bare LF or
REM    multi-byte UTF-8 comments desync its parser and make it execute a fragment of a line
REM    (observed: "'esk.app' is not recognized" from "-m quantdesk.app" split mid-token).
REM  * never let a literal backslash+b reach this file: "\b" becomes byte 0x08 and silently
REM    corrupts the path it sits in (observed: data\logs<BS>ackend_stdout.log).
chcp 65001 >nul
cd /d "%~dp0"

if not exist data\logs mkdir data\logs

REM A second backend cannot bind the port; if one is already healthy, just open the window.
powershell -NoProfile -Command "try{ (Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/system/health' -UseBasicParsing -TimeoutSec 2).StatusCode }catch{ exit 7 }"
if not errorlevel 1 (
    echo [QuantDesk] Backend already running on 127.0.0.1:8787, opening window only...
    goto open_shell
)

echo [QuantDesk] Starting backend on 127.0.0.1:8787 ...
start "QuantDesk-Backend" /min cmd /c "uv run python -m quantdesk.app >> data\logs\backend_stdout.log 2>&1"

echo [QuantDesk] Waiting for backend to become ready...
powershell -NoProfile -Command "$t0=Get-Date; while($true){ try{ $r=Invoke-WebRequest -Uri 'http://127.0.0.1:8787/api/system/health' -UseBasicParsing -TimeoutSec 1; if($r.StatusCode -eq 200){ break } }catch{}; if(((Get-Date)-$t0).TotalSeconds -gt 25){ exit 1 }; Start-Sleep -Milliseconds 500 }"
if errorlevel 1 (
    echo [QuantDesk] Backend not ready within 25s. Check data\logs\backend_stdout.log
    pause
    exit /b 1
)

:open_shell
echo [QuantDesk] Opening desktop window ...
uv run python desktop\shell.py
echo [QuantDesk] Shell exited. The backend window can be closed or left running.
