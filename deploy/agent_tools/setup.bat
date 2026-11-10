@echo off
rem ---------------------------------------------------------------------------
rem op-agent - installation on an analyst's machine. Run ONCE.
rem
rem Works COMPLETELY OFFLINE: both python and the GreenPlum driver are in this same
rem folder. The corporate proxy and TLS inspection play no part in this step at all -
rem there is nothing for them to break. A local python on the machine is neither needed nor used.
rem
rem Why an unpacked distribution and not a venv: a venv refers to the base python
rem of the machine where it was created (pyvenv.cfg -> home). Another machine has no such
rem python. A full relocatable copy has no such dependency.
rem ---------------------------------------------------------------------------
setlocal
rem The file is saved in UTF-8; without switching the code page the console (by
rem default CP866) would show all non-ASCII text as garbage.
chcp 65001 >nul
cd /d "%~dp0"

set PYVER=3.14.8
set PKG=python-%PYVER%.nupkg.zip

if exist "python\python.exe" (
    echo [1/7] Python already unpacked - skipping.
    goto :deps
)

if not exist "%PKG%" (
    echo [!] %PKG% not found next to this file.
    echo     It looks like the folder was not copied completely.
    goto :err
)

echo [1/7] Unpacking Python %PYVER% ...
powershell -NoProfile -Command "Expand-Archive -Path '%PKG%' -DestinationPath '%TEMP%\op-agent-py' -Force" || goto :err
robocopy "%TEMP%\op-agent-py\tools" "python" /e /np /nfl /ndl >nul
if errorlevel 8 goto :err
rd /s /q "%TEMP%\op-agent-py" 2>nul

:deps
echo [2/7] Checking that the kit is unpacked completely ...
rem A single check for python-*.nupkg.zip is not enough: without the mirror\ folder the agent
rem starts (/health answers 200), but every page of the report library is a 404,
rem and the log says nothing about it. The agent itself knows the list of required files (the
rem builder wrote it in from agent\mirror\), so we ask it rather than duplicate
rem the list here: a new page will not require editing this file. Python has
rem already been unpacked by now, and the check needs neither the network nor the driver.
if not exist "op_agent.py" (
    echo [!] op_agent.py not found next to this file.
    echo     It looks like the folder was not copied completely - unpack the zip again.
    goto :err
)
"python\python.exe" op_agent.py check-kit || goto :err

echo [3/7] Installing pip (from the distribution itself, offline) ...
"python\python.exe" -m ensurepip --upgrade >nul || goto :err

echo [4/7] Packages from wheels\ (offline) ...
rem The package list is NOT written here: the builder (make-agent-kit.sh) puts it into the kit
rem from the same source it downloaded the wheels from and verified them against -
rem a fourth copy of the list would one day diverge from the first three.
rem Versions of the analytics packages come from the Pyodide lock, not from the generator
rem manifest: the native run must compute on the same pandas version as
rem the browser.
if not exist "wheels\requirements.txt" (
    echo [!] wheels\requirements.txt not found - the kit was built by an old builder.
    goto :err
)
"python\python.exe" -m pip install --no-index --find-links wheels -r wheels\requirements.txt || goto :err

echo [5/7] Checking that the packages load ...
rem We check by IMPORT, not by the fact of installation: pip silently installs a wheel
rem for a foreign ABI, and it then fails on the person's machine, in the middle of a run.
"python\python.exe" -c "import psycopg, pandas, numpy, python_calamine as c; print('    psycopg', psycopg.__version__, '| pandas', pandas.__version__, '| numpy', numpy.__version__, 'OK')" || goto :err

if not exist "agent.ini" (
    rem The daemon's first start creates an agent.ini template and exits with code 1 -
    rem that is normal behaviour, not a failure, so the exit code is ignored here.
    "python\python.exe" op_agent.py >nul 2>&1
)
if not exist "agent.ini" (
    echo [!] agent.ini was not created - something is wrong with op_agent.py.
    goto :err
)

echo [6/7] Autostart at Windows sign-in ...
rem The key is written to HKCU - for the CURRENT user, without administrator rights.
rem So the installation must run under the account of whoever will be working:
rem the encrypted password lives there too (DPAPI is bound to the user).
"python\python.exe" op_agent.py install-autostart || goto :err

echo [7/7] Shortcut "OnePage Portable" (desktop and Start menu) ...
rem Same principle as autostart: everything under the CURRENT user and without administrator
rem rights. WScript.Shell writes the .lnk into the personal folders (Desktop and Programs
rem from GetFolderPath - THEIRS, not a hard-coded C:\Users\...\Desktop: the desktop
rem has often moved to OneDrive); system-wide folders are not touched at all.
rem
rem Paths are passed through an environment variable rather than embedded in the command line:
rem folder or user names can contain spaces and apostrophes, and quotes inside
rem quotes cmd -> powershell break exactly on them. The shortcut name is kept ASCII
rem for the same reason: non-ASCII letters and dashes in the -Command argument depend
rem on the console code page. The target is op-agent.bat open
rem (see the comment there), not pythonw: the shortcut must not only show the window
rem but also start the agent if it is not running. A shortcut failure does not cancel the installation: it is
rem a convenience, not a requirement, so this is a warning, not :err.
if not exist "onepage.ico" (
    echo     [!] onepage.ico not found - the shortcut will have no icon.
)
set "OPDIR=%~dp0"
powershell -NoProfile -Command "$d=$env:OPDIR; $q=[char]34; $n='OnePage Portable'; $w=New-Object -ComObject WScript.Shell; foreach($f in 'Desktop','Programs'){ $s=$w.CreateShortcut((Join-Path ([Environment]::GetFolderPath($f)) ($n+'.lnk'))); $s.TargetPath=$env:ComSpec; $s.Arguments='/c '+$q+$q+$d+'op-agent.bat'+$q+' open'+$q; $s.WorkingDirectory=$d.TrimEnd([char]92); $s.WindowStyle=7; $s.Description='OnePage'; if(Test-Path ($d+'onepage.ico')){ $s.IconLocation=$d+'onepage.ico' }; $s.Save() }; $old=-join([char[]](0x4F,0x6E,0x65,0x50,0x61,0x67,0x65,0x20,0x2014,0x20,0x43C,0x43E,0x438,0x20,0x43E,0x442,0x447,0x451,0x442,0x44B)); foreach($f in 'Desktop','Programs'){ $o=Join-Path ([Environment]::GetFolderPath($f)) ($old+'.lnk'); if(Test-Path $o){ Remove-Item $o -Force -ErrorAction SilentlyContinue } }"
if errorlevel 1 (
    echo     [!] The shortcut was not created. The library can be opened from the tray icon menu.
)

echo.
echo ============================================================
echo   Installation complete.
echo.
echo   What remains is to enter the GreenPlum login in agent.ini
echo   (the line user = CHANGE-ME..., open it in Notepad).
echo.
echo   Nothing else needs to be started: the op-agent icon will appear
echo   next to the clock by itself - now and at every Windows sign-in.
echo   The password is entered by clicking the icon. The report library is
echo   the "OnePage Portable" shortcut on the desktop.
echo ============================================================
echo.

rem Start the agent right away, so that no Windows re-login is required.
start "" "python\pythonw.exe" op_agent.py
pause
exit /b 0

:err
echo.
echo [!] INSTALLATION NOT COMPLETE. Send the line with the error above to the developer.
echo.
pause
exit /b 1
