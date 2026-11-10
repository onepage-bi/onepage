============================================================
  op-agent — copies of OnePage reports on your computer
============================================================

This text was checked against the program code on 12.10.2026. What
has and has not been tested on a real Windows machine is described
in the section "WHAT IS NOT THERE YET" — read it before the first run.


------------------------------------------------------------
  WHAT IT IS
------------------------------------------------------------

The op-agent program (the same one that works with GreenPlum) can
keep copies of the OnePage reports you have access to on your
computer. If the platform is unavailable, a copy opens and works:
filters, toggles, charts.

A copy is the latest version of the report the program managed to
fetch. It is not a replacement for the platform and not a backup
for administrators.


------------------------------------------------------------
  WHAT WORKS
------------------------------------------------------------

Copies update by themselves.
  * The first check happens 20 seconds after the program starts,
    then roughly once an hour.
  * An access key is required (section "ACCESS KEY"). Without a key
    the program downloads nothing, but what is already downloaded opens.
  * If the platform does not respond, the program retries after a
    minute, then after increasingly long pauses, but at least once an hour.
  * If the key has expired or been revoked, the program stops knocking
    every hour. A new key is needed.

You can update immediately from the icon menu: "Update now". The item
is disabled while there is no key or an update is already running.

The icon menu (right-click the icon by the clock) offers for copies:
  "Open library"            the list of copies, see "WHERE THE REPORTS ARE";
  "Show status"             what the update is doing right now and when
                            the last successful check was;
  "Add key…" or
  "Replace key…"            enter the access key.
If the log says "module agent.mirror.sync not found", these items
will not be in the menu: copies are then not updated.

Copies are more convenient to open in a separate window. The program
looks for Microsoft Edge on the computer and opens the library in its
window. If Edge is not found, an ordinary browser tab opens, and this
is written to the log.

The list of copies shows one of these captions under each report:
  Copy of <date>            when the program last reconciled the copy
                            with the platform;
  No copy yet               the report exists, but it could not be
                            downloaded yet;
  "This report shows each
  person only their own
  rows…"                    there will be no copy of such a report, see below;
  "The report is not yet
  published on the
  platform…"                no copy yet, because the report does not exist
                            on the platform in published form.

The "Open on platform" button leads to the report itself on the platform.


------------------------------------------------------------
  A REPORT FROM FOLDERS ON THIS COMPUTER
------------------------------------------------------------

Copies are for reading. Separately from them, the program can
RECALCULATE a report: read exports from folders on your disk and
upload the result to the platform. The data itself goes nowhere —
only the finished report does.

Where to take the files from is written in folders.ini next to the
program. Next to it is folders.ini.example — a sample explaining
every line; copy it as folders.ini and edit. The program does not
create its own folders.ini in advance: an empty file would look
configured.

You can see the result on the "Report folders" screen:
  * the "Report folders…" item in the icon menu;
  * or  op-agent.bat folders  from the agent folder.
For each report the screen shows the list of paths, how many files
were found and their size, and for a missing path it says plainly
what is wrong: no such path, no access, a file given instead of a
folder, or no file matched the masks.

How to run a recalculation: open the report on the platform, in the
data panel choose "Compute on this computer (op-agent)" instead of
"Compute here, in the browser", and press run. Progress goes to the
same page as for a browser run; the result is published the same way.

An empty result is NEVER considered a success. If no file matched
the masks or a folder has disappeared, the program refuses to compute
and names the reason. A silent run on nothing would publish an empty
report, and there would be nobody left to find out about it.

Platform down? The run still goes ahead. The result is stored next
to the copies as a local version marked as unpublished, and waits.
When the platform comes back, the program asks whether to upload
it — it never does so by itself without asking. The version note on
the platform will say that the report was built offline and on which date.

The platform sees the folder paths — that is the point: otherwise two
Excel files from two departments are indistinguishable. They are seen
by whoever can already update this report's data; readers and
share-link viewers are not shown the paths, and they do not go into
requests to the model.

An important note on numbers: a local run computes with REAL Python,
and a browser run with the same Python inside the browser. On our
report the numbers matched to the last digit, but that is a statement
about OUR report, not about generators in general. A new generator
that will be computed locally should be checked once against a
browser run of the same folder.


------------------------------------------------------------
  WHICH REPORTS HAVE NO COPIES
------------------------------------------------------------

There are two cases. In both the program neither downloads nor keeps a copy.

1. A report with row-level access. Some reports show each person
   only their own data — for example, only their territory or
   positions. Such a report has no single copy of "the whole report",
   so there will be none on your computer: otherwise it would hold
   rows you are not supposed to see. If such a report was copied
   earlier, the copy is deleted at the next update. The report stays
   in the list, with a note and an "Open on platform" link. This is
   not a fault.

2. A report that is not published on the platform. It has no copy
   until it is published.


------------------------------------------------------------
  WHAT IS NOT THERE YET
------------------------------------------------------------

The program has not been tested on Windows. It was developed and
tested on macOS, and nobody has run it on a real Windows machine yet.
Not tested: scheduled updates, key entry, the Edge window, the
desktop and Start menu shortcut (created by setup.bat). The first
surprises will surface on your side. If something is wrong, tell IT
and send the log (see "WHERE THE REPORTS ARE").

There is no picking folders with the mouse. The "Report folders"
screen shows what is configured and checks whether the paths exist —
but the paths themselves are still typed into folders.ini as text.

There are no nightly report runs. The program does not run exports
from GreenPlum on a schedule. Updating copies from the platform
happens once an hour; that is a different thing.

There are no "Up to date" and "A newer one is on the platform"
captions. The program does not query the platform while working
offline, and so it shows only the date of the copy.


------------------------------------------------------------
  ACCESS KEY
------------------------------------------------------------

The key lets the program update copies without you.

Where to get it: on the platform open your login in the bottom-left
corner of the menu — this is the "My account" page. In the section
"Keys for the desktop program" press "Issue key".

Important:
  * the key is shown ONCE, right after it is issued. The page says
    so: "Copy it now — it will not be shown again." Missed it — issue
    a new one. Revoke the old one in the same place.
  * the key gives the program READ access only. It cannot be used to
    change a report or publish anything.
  * the key is valid for 90 days. The expiry date is shown on the page
    when it is issued. After that a new key is needed.
  * the key is access to your reports. Do not send it in emails or chats.

Where to enter it: icon → "Add key…" (or "Replace key…" if there
already was one). It can also be done from the library window. Paste
the key and confirm. The program stores it encrypted under your
Windows account, just like the GreenPlum password. Right after saving
it starts an update.

If the key is not accepted: check that it was copied in full, with
no spaces or line breaks. If that does not help, issue a new one.

Reports already downloaded open without a key, and also after the
key has expired.


------------------------------------------------------------
  WHERE THE REPORTS ARE
------------------------------------------------------------

The list of copies opens from the icon menu ("Open library").
If you need to open it manually, the address in the browser is:

    http://127.0.0.1:47113/library/

The program takes the first free port of 47113, 47114, 47115.
If 47113 does not open, try 47114 or 47115.

The files themselves are in a service folder:
    %LOCALAPPDATA%\op-agent\mirror

Its contents are managed by the program. Do not move or edit the
files in it by hand.

Log: %LOCALAPPDATA%\op-agent\op-agent.log. It can be opened with
the "Show log" item in the icon menu. The access key is never
written to the log.


------------------------------------------------------------
  FOR IT
------------------------------------------------------------

Network connections:
  * inbound: only 127.0.0.1 (this computer), ports 47113–47115.
    The library pages accept requests only from their own address.
    A request from another site is rejected (403), and the key is
    not saved in that case.
  * outbound (copy updates): HTTPS to the platform. The address comes
    from the OP_MIRROR_SERVER variable, or if it is not set — the first
    address in agent.ini, section [security], parameter allowed_origins.
    GET requests only, no cookies. A redirect to another host is refused.

What is stored on disk (%LOCALAPPDATA%\op-agent):
  * copies of reports and files as the platform served them. They may
    contain report data;
  * an inventory: versions, permission expiry dates, reasons why a
    report was not downloaded;
  * the access key — only in encrypted form (Windows DPAPI,
    file creds\onepage-device-key.bin, bound to the user account);
  * the log and the schedule state (mirror-schedule.json).

What is NOT stored:
  * the access key in plain form;
  * the GreenPlum password (it is kept separately, also in DPAPI);
  * copies of reports with row-level access;
  * OnePage cookies and sessions.

Folder layout: a mirror\ folder (texts and pages of the library) must
sit next to op_agent.py. Without it the program starts, GreenPlum
works, but the library pages answer "not found".

Revoking the key on the platform stops copy updates. Copies already
downloaded keep opening.

Full mirror cleanup: close the program, delete the folder
%LOCALAPPDATA%\op-agent\mirror and the file
%LOCALAPPDATA%\op-agent\creds\onepage-device-key.bin.
