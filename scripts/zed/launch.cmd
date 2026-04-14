@echo off
:: Aar ACP launcher — invoked by Zed on Windows.
::
:: Zed passes the desired port via ZED_AGENT_PORT; we fall back to 8000.

setlocal

set "PORT=%ZED_AGENT_PORT%"
if "%PORT%"=="" set "PORT=8000"

set "HOST=%ZED_AGENT_HOST%"
if "%HOST%"=="" set "HOST=127.0.0.1"

:: Ensure aar is available; install from PyPI on first run.
where aar >nul 2>&1
if errorlevel 1 (
    echo [aar-zed] aar command not found -- installing aar-agent from PyPI... 1>&2
    python -m pip install --quiet --user "aar-agent>=0.3.2"
)

aar acp --host "%HOST%" --port "%PORT%"
