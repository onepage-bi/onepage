============================================================
  op-agent — GreenPlum reports in one click
============================================================

What it is. A small program that lives on your computer as an icon next to
the clock. When you press "Run" in OnePage, it executes a pre-approved
query in GreenPlum UNDER YOUR PERSONAL LOGIN and hands the result to the
page that assembles the finished report.

Two consequences that explain why it is built this way:
  * the OnePage server never stores or sees your GreenPlum password;
  * in the GreenPlum audit the query is attributed to you, not to a shared account.


------------------------------------------------------------
  INSTALLATION  (once, ~2 minutes)
------------------------------------------------------------

IMPORTANT: install it under your own Windows account — the one you work
under. The password is encrypted by Windows and bound to the user; an
install done "for you" by someone else will not work.

1. Copy this whole folder to disk — for example to C:\OpAgent
   Copy it; do not run it straight from the archive.

2. Run   setup.bat
   No internet needed: both Python and the driver are in this folder.
   At the end you should see "Installation complete", and the op-agent
   icon appears next to the clock.

3. Open  agent.ini  (in Notepad) and enter your GreenPlum login:

       user = CHANGE-ME-your-gp-login   ->   user = u10000009

   Everything else (server address, database) is already filled in — the
   same values as in DBeaver's "Connection settings" window.

   THE PASSWORD DOES NOT GO INTO THIS FILE. Ever. That is the next step.

4. Click the op-agent icon by the clock → "Enter GreenPlum password".
   Type the password and press "Save".

   The agent immediately checks it with a real connection and tells you
   whether it worked. The icon turns green.


------------------------------------------------------------
  HOW TO USE IT
------------------------------------------------------------

There is nothing to start — the icon appears by itself when you sign in to Windows.

1. In OnePage open your report, the "Data" panel on the right, the "Run" button.

2. The VERY FIRST time a question pops up: "allow this site to run
   reports?" — press "Yes". It will not appear again.

   Answer "Yes" only if you yourself have just pressed "Run".

3. You then see how many rows were exported, after which the report is
   assembled and published. The icon is blue meanwhile.

4. When everything is done, the icon shows a "Report ready" popup.

Icon colour:
    grey     GreenPlum password not set
    green    ready
    blue     data export in progress
    red      the last attempt failed


------------------------------------------------------------
  IF SOMETHING IS WRONG
------------------------------------------------------------

Right-click the icon — everything you need is there:

  "Test connection"          — says in full what exactly is wrong
                               (no network, wrong password, no access).
  "Enter GreenPlum password" — if the password has changed.
  "Show log"                 — details; this file can be sent to the
                               developer, it contains no password.
  "Unlink OnePage"           — revoke the permission; the next report
                               run will ask for it again.

No icon at all? Open the agent folder and run setup.bat again.

Common messages:

  "Agent op-agent not found"
      The agent is not running. Run setup.bat again.
  "Password not set"
      Step 4 of the installation.
  "GreenPlum rejected the login or password"
      Check user in agent.ini and enter the password again.
  "no connection to GreenPlum"
      The server is not reachable from this machine: network, VPN, access rights.
  "query exceeded statement_timeout"
      The query runs too long — tell the developer.


------------------------------------------------------------
  FOR IT: console commands
------------------------------------------------------------

Everything the icon does is also available as commands — for setup and
troubleshooting. Run them from the agent folder:

  op-agent.bat test-conn analytics_dw     test the connection (full text)
  op-agent.bat set-cred  analytics_dw     save the password
  op-agent.bat list-cred               under which names passwords are saved
  op-agent.bat del-cred  analytics_dw     delete the password
  op-agent.bat install-autostart       enable autostart (HKCU, no admin rights)
  op-agent.bat remove-autostart        disable autostart
  op-agent.bat --no-tray               run without the icon, output to console
  op-agent.bat version                 version and build stamp
  op-agent.bat check-kit               whether all kit files are in place (mirror folder)
  op-agent.bat folders                 the "Report folders" screen in its own window

A limitation that is easy to forget: both the password (DPAPI) and the
autostart (HKCU) belong to a SPECIFIC Windows user. Setting it up under
someone else's account will not work for the employee. In any case the
employee enters the password themselves — it is personal, and that is the
whole point of the scheme.

What is in the folder:

  setup.bat                installation (once)
  op-agent.bat             commands for IT
  op_agent.py              the agent itself (a single file, readable source)
  mirror\                  texts and pages of the report-copy library;
                           they sit next to op_agent.py and it needs them as is.
                           Without it the agent does not start: it shows what
                           is missing and exits (code 3) - unpack the zip again
  README-MIRROR.txt        about report copies on this computer
  folders.ini.example      sample folder configuration: copy it alongside as
                           folders.ini and edit. The program does not create
                           folders.ini itself
  appwin\                  pages of the "Report folders" screen
  r2host.py                runs the generator in a separate process; without it
                           a run from folders fails with gen_no_host
  _lineage.py              lineage probe: which sheets and columns the
                           generator actually opened
  agent.ini                connection settings — appears after setup.bat,
                           it contains no password and must not
  python\                  portable Python — appears after setup.bat
  wheels\                  GreenPlum driver for offline installation
  python-3.14.8.nupkg.zip  Python distribution

The password is NOT stored in the folder — it is in the protected Windows
store (%LOCALAPPDATA%\op-agent\creds), encrypted for the user account.
The log is there too: %LOCALAPPDATA%\op-agent\op-agent.log
