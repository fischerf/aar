@echo off
:: Aar ACP launcher -- invoked by Zed on Windows.
::
:: Zed launches this script over stdio.  ZED_AGENT_PORT / ZED_AGENT_HOST are
:: only meaningful when `aar acp --http` is in use; in stdio mode (the default
:: used by Zed) they are harmless extra flags.
::
:: This script assumes `aar` is already on PATH.  aar-agent is not yet
:: published to PyPI, so installation must happen out-of-band -- see the error
:: message below for the supported install command.

setlocal

set "PORT=%ZED_AGENT_PORT%"
if "%PORT%"=="" set "PORT=8000"

set "HOST=%ZED_AGENT_HOST%"
if "%HOST%"=="" set "HOST=127.0.0.1"

where aar >nul 2>&1
if errorlevel 1 (
    echo [aar-zed] ERROR: 'aar' command not found on PATH. 1>&2
    echo. 1>&2
    echo   aar-agent is not yet published to PyPI, so the Zed launcher cannot 1>&2
    echo   install it for you.  Please install it from source first: 1>&2
    echo. 1>&2
    echo       pip install --user "git+https://github.com/fischerf/aar.git@v0.4.0#egg=aar-agent[all]" 1>&2
    echo. 1>&2
    echo   or, for a local development checkout: 1>&2
    echo. 1>&2
    echo       git clone https://github.com/fischerf/aar.git 1>&2
    echo       cd aar 1>&2
    echo       pip install -e ".[all]" 1>&2
    echo. 1>&2
    echo   After installation, confirm 'aar --help' works in a fresh shell, 1>&2
    echo   then reload the Zed agent server. 1>&2
    echo. 1>&2
    echo   See https://github.com/fischerf/aar#installation for full instructions. 1>&2
    exit /b 127
)

aar acp --host "%HOST%" --port "%PORT%"
