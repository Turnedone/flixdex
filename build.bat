@echo off
rem Builds dist\Flixdex.exe using the Python environment in .venv (created on first run).
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
set "WORK=%~dp0build"

if not exist "%PY%" (
  echo Setting up build environment...
  python -m venv "%~dp0.venv" || exit /b 1
  "%PY%" -m pip install --disable-pip-version-check PySide6 pyinstaller || exit /b 1
)
if not exist "%WORK%" mkdir "%WORK%"

"%PY%" -c "from PySide6.QtWidgets import QApplication; a = QApplication([]); import flixdex; flixdex.app_icon().pixmap(256, 256).save(r'%WORK%\flixdex.ico')" || exit /b 1

rem --clean: a stale cached analysis once produced an exe without Qt's plugins (no HTTPS, so nothing loaded).
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name Flixdex ^
  --icon "%WORK%\flixdex.ico" ^
  --distpath "%~dp0dist" --workpath "%WORK%" --specpath "%WORK%" ^
  "%~dp0flixdex.py" || exit /b 1

rem Refuse to ship an exe without Qt's HTTPS and window plugins.
findstr /c:"qschannelbackend" "%WORK%\Flixdex\PKG-00.toc" >nul || (echo ERROR: Qt TLS plugin missing from build & exit /b 1)
findstr /c:"qwindows" "%WORK%\Flixdex\PKG-00.toc" >nul || (echo ERROR: Qt windows plugin missing from build & exit /b 1)

echo.
echo Built: %~dp0dist\Flixdex.exe
