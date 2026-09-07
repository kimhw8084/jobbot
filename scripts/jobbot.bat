@echo off
setlocal
set "ROOT=%~dp0.."
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo Run: py -m venv .venv ^& .venv\Scripts\python -m pip install -e . 1>&2
  exit /b 2
)
"%PY%" -m jobbot %*
exit /b %ERRORLEVEL%
