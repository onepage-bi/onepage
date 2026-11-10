@echo off
rem ---------------------------------------------------------------------------
rem op-agent - single entry point.
rem
rem   op-agent.bat                      start the daemon (do not close the window)
rem   op-agent.bat set-cred  analytics_dw  save a password in Windows DPAPI
rem   op-agent.bat test-conn analytics_dw  test the connection to GreenPlum
rem   op-agent.bat list-cred            where passwords are saved
rem   op-agent.bat version              version and build stamp
rem   op-agent.bat diagnose             ports, config and the tail of the log
rem   op-agent.bat s3-list   ldwh       what is in the S3 bucket (downloading nothing)
rem   op-agent.bat s3-sync   ldwh       download the S3 bucket into a local folder
rem   op-agent.bat open                 start the agent (if not running) and
rem                                     open the library window; the shortcut's target
rem
rem The daemon runs on the PORTABLE python from the neighbouring folder. This is not cosmetic:
rem the agent runs the report generator as a separate process via sys.executable
rem (isolation: a generator crash does not kill the daemon, cancellation stays clean), and
rem sys.executable must be a real python. Hence no .exe either:
rem a PyInstaller wrapper would substitute itself and, on top of that, add an
rem unsigned binary to a corporate machine where a .bat is enough.
rem ---------------------------------------------------------------------------
rem UTF-8: see the comment in setup.bat.
chcp 65001 >nul
cd /d "%~dp0"

if not exist "python\python.exe" (
    echo.
    echo [!] python\python.exe not found - run setup.bat first
    echo.
    pause
    exit /b 1
)

set "OPWIN=library"
if /i "%~1"=="open" goto :open
rem The "Report folders" screen: the same wait for the agent and the same window/tab choice
rem as the library, so there is one branch, and only OPWIN tells them apart. A separate
rem copy of these twenty lines would diverge from the original on the very first edit.
if /i "%~1"=="folders" (
    set "OPWIN=folders"
    goto :open
)

"python\python.exe" op_agent.py %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
    echo.
    echo [exit code %RC%]
    pause
)
exit /b %RC%

:open
rem Target of the "OnePage - my reports" shortcut. Why not a shortcut straight to pythonw.exe:
rem pythonw op_agent.py only STARTS the agent (and with one already running it silently exits -
rem the second instance runs into the busy port); it does not open a window. The shortcut
rem must do both steps: the agent may not have started (the person closed it, the Windows
rem sign-in has not finished yet), and then a click "does nothing", which is worse than any
rem error. op_agent.py itself has no open subcommand, and an unknown command there is
rem a refusal with code 2, so the window is opened by a short script on top of the module.
rem
rem Steps: (1) start - the agent in the background, without a window; (2) wait up to 20 s for /health
rem to answer as our agent (the first start is slower: unpacking, antivirus);
rem (3) appwindow.open_window takes the address from mirror_schedule.library_url - the string
rem "/library/" is not duplicated, and the same window/tab choice path with a log
rem entry as the tray icon menu item. The script runs under pythonw: there will be no black
rem console window (the cmd window itself is hidden by the shortcut's WindowStyle).
start "" "python\pythonw.exe" op_agent.py
"python\pythonw.exe" -c "import os,sys,time;sys.path.insert(0,'.');import op_agent as a;a.applog.setup(a.state_dir());p=lambda:next((x for x in a.PORT_LADDER if a._is_our_agent(x)),0);any(p() or time.sleep(.5) for _ in range(40));n=p();sys.exit(0 if n and (a.appwindow.open_folders(n,state_root=a.state_dir()) if os.environ.get('OPWIN')=='folders' else a.appwindow.open_window(a.mirror_schedule.library_url(n),state_root=a.state_dir(),title='OnePage')) else 3)"
if errorlevel 1 (
    echo.
    echo [!] The agent did not respond within 20 seconds. Below is its startup log.
    echo     If there is no log, the agent did not even reach its first line: then
    echo     run  op-agent.bat --no-tray  and send the whole output.
    echo.
    rem Diagnostics are printed with the CONSOLE python: pythonw above wrote to a file, and without
    rem this step the person was told to open the log from the tray icon menu - that
    rem is, from the icon that is exactly what is missing after a failed start.
    "python\python.exe" op_agent.py diagnose
    echo.
    pause
    exit /b 1
)
exit /b 0
