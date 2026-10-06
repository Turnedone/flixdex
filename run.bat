@echo off
rem Runs Flixdex from source (run build.bat once first to set up .venv).
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0flixdex.py"
