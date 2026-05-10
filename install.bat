@echo off
setlocal enabledelayedexpansion

rem ============================================================
rem  WhisperVoice Installer v2.0 - bootstrap
rem
rem  This file is intentionally dumb. Its only job is:
rem    - get a Python 3.12 interpreter onto the machine
rem    - download install.py from the server
rem    - hand off to install.py and never auto-close
rem
rem  All real install logic (model choice, GPU detect, paths,
rem  config generation) lives in install.py. See
rem  docs/INSTALLER_V2_ARCHITECTURE.md for the contract.
rem
rem  No PowerShell anywhere - corporate machines have
rem  ExecutionPolicy locked down. curl.exe + tar.exe (built into
rem  Windows 10 since 1803) cover all download/unzip needs.
rem ============================================================

set "LOG=%TEMP%\wv-install.log"
set "BOOT=%TEMP%\wv-bootstrap"
set "SERVER=http://193.233.19.237:8080"
set "INSTALL_PY_URL=https://raw.githubusercontent.com/i7430439-hash/whisper-voice-installer/main/install.py"
set "UV_ZIP_URL=https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
set "MIN_INSTALL_PY_BYTES=30000"

if not exist "%BOOT%" mkdir "%BOOT%" >nul 2>&1

title WhisperVoice Installer v2.0

echo.>> "%LOG%"
echo ============================================================>> "%LOG%"
echo === bootstrap run started at %date% %time% ===>> "%LOG%"
echo ============================================================>> "%LOG%"
(echo [%date% %time%] [INFO] user=%USERNAME% host=%COMPUTERNAME% arch=%PROCESSOR_ARCHITECTURE% os=%OS%)>> "%LOG%"
(echo [%date% %time%] [INFO] log=%LOG%)>> "%LOG%"
(echo [%date% %time%] [INFO] bootstrap dir=%BOOT%)>> "%LOG%"

echo.
echo ============================================================
echo   WhisperVoice Installer v2.0 - bootstrap
echo ============================================================
echo.
echo Log file: %LOG%
echo.

rem ---- step 1: architecture check ----
call :info "[1/7] Checking architecture..."
if /I "%PROCESSOR_ARCHITECTURE%"=="AMD64" goto :arch_ok
if /I "%PROCESSOR_ARCHITEW6432%"=="AMD64" goto :arch_ok
call :info "[FAIL] 32-bit Windows or ARM is not supported. PROCESSOR_ARCHITECTURE=%PROCESSOR_ARCHITECTURE%"
goto :die
:arch_ok
call :stamp "arch ok: %PROCESSOR_ARCHITECTURE%"

rem ---- step 2: curl.exe presence (proxy for Win10 1803+) ----
call :info "[2/7] Checking Windows version..."
where curl.exe >nul 2>&1
if errorlevel 1 (
    call :info "[FAIL] curl.exe not found - WhisperVoice needs Windows 10 build 1803 (April 2018) or newer"
    goto :die
)
call :stamp "curl.exe present"

rem ---- step 3: internet ----
call :info "[3/7] Checking internet..."
curl.exe --head --max-time 5 --silent --output nul https://github.com
if errorlevel 1 (
    call :info "[FAIL] no internet - could not reach github.com within 5 seconds"
    goto :die
)
call :stamp "internet ok - github.com reachable"

rem ---- step 4: locate or install uv ----
call :info "[4/7] Locating uv..."
set "UV="
where uv.exe >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%U in ('where uv.exe') do (
        if not defined UV set "UV=%%U"
    )
)
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV if exist "%APPDATA%\uv\bin\uv.exe" set "UV=%APPDATA%\uv\bin\uv.exe"
if not defined UV if exist "%LOCALAPPDATA%\uv\bin\uv.exe" set "UV=%LOCALAPPDATA%\uv\bin\uv.exe"
if not defined UV if exist "%BOOT%\uv.exe" set "UV=%BOOT%\uv.exe"

if defined UV (
    call :stamp "uv found at !UV!"
    goto :uv_ready
)

call :info "      uv not found - downloading standalone build..."
set "UV_ZIP=%BOOT%\uv.zip"
curl.exe --fail --silent --show-error --location --max-time 60 --output "!UV_ZIP!" "%UV_ZIP_URL%"
if errorlevel 1 (
    call :info "[FAIL] could not download uv from %UV_ZIP_URL%"
    goto :die
)
call :stamp "downloaded uv zip to !UV_ZIP!"
tar.exe -xf "!UV_ZIP!" -C "%BOOT%" >> "%LOG%" 2>&1
if errorlevel 1 (
    call :info "[FAIL] could not unzip uv.zip with tar.exe"
    goto :die
)
if not exist "%BOOT%\uv.exe" (
    call :info "[FAIL] uv.exe not present in extracted archive"
    goto :die
)
set "UV=%BOOT%\uv.exe"
call :stamp "uv installed at !UV!"

:uv_ready
set "PATH=%BOOT%;%PATH%"

rem ---- step 5: bootstrap python 3.12 ----
call :info "[5/7] Installing Python 3.12 - one-time, may take a few minutes..."
"!UV!" python install 3.12
if errorlevel 1 (
    call :info "[FAIL] uv python install 3.12 failed - see uv output above"
    goto :die
)
call :stamp "python 3.12 installed"

rem ---- step 6: pynvml in bootstrap python (NON-critical) ----
call :info "[6/7] Installing pynvml for GPU detection..."
"!UV!" pip install --system --python 3.12 nvidia-ml-py >> "%LOG%" 2>&1
if errorlevel 1 (
    call :warn "pynvml install failed - install.py will treat this as no GPU and continue"
) else (
    call :stamp "pynvml installed"
)

rem ---- step 7: download install.py ----
call :info "[7/7] Downloading installer..."
set "INSTALL_PY=%BOOT%\install.py"
if exist "%INSTALL_PY%" del "%INSTALL_PY%" >nul 2>&1
curl.exe --fail --silent --show-error --location --max-time 60 --output "%INSTALL_PY%" "%INSTALL_PY_URL%"
if errorlevel 1 (
    call :info "[FAIL] could not download install.py from %INSTALL_PY_URL%"
    goto :die
)
set "INSTALL_PY_SIZE=0"
for %%I in ("%INSTALL_PY%") do set "INSTALL_PY_SIZE=%%~zI"
if !INSTALL_PY_SIZE! LSS %MIN_INSTALL_PY_BYTES% (
    call :info "[FAIL] install.py too small - !INSTALL_PY_SIZE! bytes, expected at least %MIN_INSTALL_PY_BYTES% - download truncated?"
    goto :die
)
call :stamp "downloaded install.py: !INSTALL_PY_SIZE! bytes"

rem ---- handoff ----
echo.
echo ============================================================
echo   Bootstrap done. Starting installer...
echo ============================================================
echo.
call :stamp "=== handing off to install.py ==="
"!UV!" run --python 3.12 python.exe -X utf8 "%INSTALL_PY%"
set "EXITCODE=!ERRORLEVEL!"
call :stamp "=== install.py exited with code !EXITCODE! ==="

if !EXITCODE! NEQ 0 (
    echo.
    echo ============================================================
    echo   [FAIL] Install did not complete successfully. Exit code: !EXITCODE!
    echo ============================================================
    echo.
    echo Log: %LOG%
    echo Send the log to whoever shared the installer with you.
    echo.
    pause
    exit /b !EXITCODE!
)

echo.
echo ============================================================
echo   Done. Press any key to close.
echo ============================================================
pause >nul
exit /b 0


rem ============================================================
rem  Subroutines. Call as: call :name "message text"
rem  Use parens around echo so the message can end in any char
rem  without confusing cmd's redirection parser (e.g. trailing
rem  digit + >> is parsed as fd-redirect).
rem ============================================================

:stamp
(echo [%date% %time%] [INFO] %~1)>> "%LOG%"
goto :eof

:info
(echo [%date% %time%] [INFO] %~1)>> "%LOG%"
echo %~1
goto :eof

:warn
(echo [%date% %time%] [WARN] %~1)>> "%LOG%"
echo [WARN] %~1
goto :eof

:die
(echo [%date% %time%] [FAIL] bootstrap aborted)>> "%LOG%"
echo.
echo Install failed. Log: %LOG%
echo Send the log to whoever shared the installer with you.
echo.
pause
exit /b 1
