@echo off
rem Start LocalSeedr on Windows. First run sets up a private Python environment in .venv.
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto :install

rem libtorrent publishes ready-made packages for Python 3.9 to 3.13 only
set "PY="
for %%v in (3.13 3.12 3.11 3.10 3.9) do (
  if not defined PY (
    py -%%v -c "" >nul 2>nul && set "PY=py -%%v"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if (3,9)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY goto :nopython

echo Setting up LocalSeedr for the first time (this takes a minute)...
%PY% -m venv .venv || goto :fail

:install
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt || goto :fail
".venv\Scripts\python.exe" app.py
echo.
echo LocalSeedr has stopped.
pause
exit /b 0

:nopython
echo Python 3.9 to 3.13 is required.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this again.
pause
exit /b 1

:fail
echo Setup failed. See the message above.
pause
exit /b 1
