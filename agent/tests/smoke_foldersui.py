"""Smoke test of the "Report folders" screen: `agent/foldersui.py` over HTTP.

Run:  python -m agent.tests.smoke_foldersui   (from the OnePage repo root)

The BOUNDARY is checked, not the layout: what is served at an address, what
is rejected, and, most importantly, that what the screen shows matches what
the run will read. A screen that shows some files while the run reads others
is worse than no screen at all.

A real server is brought up (`AgentServer` on 127.0.0.1:0), otherwise the
Host and Origin checks would be checking themselves.

Standard library only.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from agent import folders, foldersui
from agent.config import Config
from agent.pairing import Pairing
from agent.server import AgentServer

# Repository root: agent/tests/<file> -> up three levels.
ROOT = Path(__file__).resolve().parents[2]

_PASS = 0
_FAIL = 0


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


class Client:
    def __init__(self, port):
        self.port = port

    def req(self, method, path, obj=None, origin=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if host:
            headers["Host"] = host
        if origin:
            headers["Origin"] = origin
        body = None
        if obj is not None:
            body = json.dumps(obj).encode("utf-8")
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        data = r.read()
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = None
        return r.status, dict(r.getheaders()), data, parsed


INI = """\
; my notes
[folders:17]
dir = {d1}
exclude = ~$*
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="op-foldersui-test-"))
    d1 = tmp / "Ventes_été"
    d2 = tmp / "Shared"
    d1.mkdir()
    d2.mkdir()
    (d1 / "sl.xlsx").write_text("x", encoding="utf-8")
    (d1 / "~$sl.xlsx").write_text("x", encoding="utf-8")
    (d2 / "extra.csv").write_text("x", encoding="utf-8")
    ini = tmp / "folders.ini"
    ini.write_text(INI.format(d1=d1), encoding="utf-8")

    saved = os.environ.get("OP_FOLDERS_INI")
    os.environ["OP_FOLDERS_INI"] = str(ini)
    # The bucket panel writes to ANOTHER file and to the credential store. We
    # redirect both to tmp: a test that puts a secret into a person's real
    # store will one day leave it there. `LOCALAPPDATA` is the same handle
    # that `config.state_dir` uses on Windows.
    agent_ini = tmp / "agent.ini"
    agent_ini.write_text("[security]\nallowed_origins = https://x\n",
                         encoding="utf-8")
    saved_env = {k: os.environ.get(k) for k in
                 ("OP_AGENT_INI", "LOCALAPPDATA", "OP_AGENT_DEV_CRED_STORE")}
    os.environ["OP_AGENT_INI"] = str(agent_ini)
    os.environ["LOCALAPPDATA"] = str(tmp / "appdata")
    os.environ["OP_AGENT_DEV_CRED_STORE"] = "1"

    cfg = Config(allowed_origins=("https://onepage.example.com",), connections={})
    srv = AgentServer(("127.0.0.1", 0), cfg, Pairing(code_factory=lambda: "123456"))
    srv.state_root = tmp / "state"
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.1)
    cl = Client(port)
    me = f"http://127.0.0.1:{port}"

    try:
        # ---------------------------------------------------------------- #
        # 1. The page and resources.
        # ---------------------------------------------------------------- #
        st, hd, body, _ = cl.req("GET", "/folders-ui/")
        check("the screen page is served", st == 200 and b"folders.js" in body, st)
        check("the page is not cached (the screen shows the disk state)",
              hd.get("Cache-Control") == "no-store", hd.get("Cache-Control"))
        st, _, _, _ = cl.req("GET", "/folders-ui")
        check("an address without a slash is a redirect, not a bare page", st == 301, st)
        for name, mime in (("folders.js", "text/javascript"),
                           ("folders.css", "text/css"),
                           ("strings.json", "application/json")):
            st, hd, b, _ = cl.req("GET", "/folders-ui/" + name)
            # A `.js` with type application/octet-stream is silently blocked
            # by ORB: the same trap as with the mirror's serving.
            check(f"{name} is served with the right type",
                  st == 200 and b and hd.get("Content-Type", "").startswith(mime),
                  (st, hd.get("Content-Type")))
        st, _, _, _ = cl.req("GET", "/folders-ui/folders.html")
        check("the page is NOT served under its own name (only by the address)",
              st == 404, st)
        st, _, _, _ = cl.req("GET", "/folders-ui/../server.py")
        check("escaping the resource directory is a 404, not a file read",
              st == 404, st)
        st, _, _, _ = cl.req("GET", "/folders-ui/%2e%2e%2fserver.py")
        check("a percent-encoded escape is a 404 too", st == 404, st)

        # Texts must exist for EVERY key that the page builds from code:
        # `reason_<reason>` and `err_<code>` are listed nowhere else, and the
        # loss of one is visible only this way.
        strings = json.loads((Path(foldersui.win_res_path("strings.json"))).read_text("utf-8"))
        for reason in ("not_found", "denied", "not_a_dir", "no_match"):
            check(f"there is a text for the missing-path reason {reason}",
                  f"reason_{reason}" in strings)
        for code in ("bad_path", "bad_mask", "bad_scenario", "bad_dashboard",
                     "bad_index", "no_section", "duplicate_section",
                     "scenario_exists", "section_exists", "unparsable",
                     "write_failed", "too_large", "not_utf8", "bad_origin"):
            check(f"there is a text for the refusal {code}", f"err_{code}" in strings)
        check("the screen's resource set is intact", foldersui.window_missing_kit() == (),
              foldersui.window_missing_kit())

        # Texts and the page must converge in BOTH directions. A key without a
        # string is printed on screen as the key name (better than emptiness,
        # but still a breakage), and a string without a key is a text the
        # person will never see, and it quietly goes stale. The same rule as
        # the mirror's
        # (`smoke_mirror_wiring`).
        import re as _re
        js = Path(foldersui.win_res_path("folders.js")).read_text("utf-8")
        # `fmt('reason_' + m.reason)` gives the stub `reason_`: the families
        # built from code are listed below by hand and removed from the finds.
        used = {k for k in _re.findall(r"fmt\('([a-z0-9_]+)'", js)
                if not k.endswith("_")}
        used |= {"reason_" + r for r in ("not_found", "denied", "not_a_dir",
                                         "no_match")}
        used |= {"err_" + c for c in ("bad_path", "bad_mask", "bad_scenario",
                                      "bad_dashboard", "bad_kind", "bad_index",
                                      "no_section", "duplicate_section",
                                      "scenario_exists", "section_exists",
                                      "unparsable", "write_failed",
                                      "read_failed", "too_large", "not_utf8",
                                      "bad_origin", "bad_json",
                                      "body_too_large",
                                      # bucket panel: the codes are built from
                                      # `errText`, there are no literals in the code
                                      "bad_s3_name", "bad_s3_host",
                                      "bad_s3_host_port", "bad_s3_port",
                                      "bad_s3_remote", "bad_s3_access_key",
                                      "bad_s3_region", "bad_s3_secret",
                                      "no_s3_secret", "cred_unavailable",
                                      "s3_failed")}
        used |= {"problem_" + c for c in ("unparsable", "duplicate_section",
                                          "too_large", "not_utf8",
                                          "read_failed")}
        used |= {"state_" + c for c in ("dir", "file", "missing", "denied")}
        used |= {"entry_dir", "entry_file"}
        check("every key of the page is in strings.json",
              not (used - set(strings)), sorted(used - set(strings)))
        # In the reverse direction we look at ALL string literals of the page: a
        # key may reach `fmt` from an array or a ternary, and a regexp over the
        # call will not see it, and a live text must not be accused of being dead.
        literals = set(_re.findall(r"'([A-Za-z0-9_]+)'", js)) | used
        check("strings.json has no strings that have nowhere to be shown",
              not (set(strings) - literals), sorted(set(strings) - literals))

        # ---------------------------------------------------------------- #
        # 2. State.
        # ---------------------------------------------------------------- #
        st, _, _, j = cl.req("GET", "/folders-ui/state.json")
        sec = j["sections"][0]
        check("the state names the same ini as the run",
              st == 200 and j["ini"] == os.path.abspath(str(ini)), j.get("ini"))
        check("the section is counted: one file, the Excel junk cut off by the mask",
              sec["checked"] and sec["files"] == 1, sec)
        check("the masks are visible on the screen", sec["exclude"] == ["~$*"], sec["exclude"])
        check("the entry shows what lies at the path",
              sec["entries"][0]["state"] == "dir", sec["entries"])
        check("the settings file is fit for writing, comments will survive an edit",
              j["writable"] and j["comments_preserved"] and j["problem"] is None, j)

        # ---------------------------------------------------------------- #
        # 3. Edits, and the display matching the run.
        # ---------------------------------------------------------------- #
        st, _, _, j = cl.req("POST", "/folders-ui/entry/add",
                             {"key": "17", "kind": "dir", "raw": str(d2)},
                             origin=me)
        sec = j["state"]["sections"][0]
        check("the folder was added, the response brought the whole fresh state",
              st == 200 and j["ok"] and sec["files"] == 2, j)
        check("what the screen shows matches what the run will read",
              [e.raw for e in folders.load(ini)["17"].entries]
              == [e["raw"] for e in sec["entries"]], sec["entries"])
        check("the person's comment is in place after an edit over HTTP",
              "; my notes" in ini.read_text("utf-8"))

        st, _, _, j = cl.req("POST", "/folders-ui/masks",
                             {"key": "17", "include": ["*.xlsx"],
                              "exclude": ["~$*"]}, origin=me)
        sec = j["state"]["sections"][0]
        check("masks rewritten: the csv of the second folder is no longer read",
              sec["files"] == 1 and sec["include"] == ["*.xlsx"]
              and sec["exclude"] == ["~$*"], sec)
        check("a folder without suitable files does NOT stay silent: there is a no_match reason",
              any(m["reason"] == "no_match" for m in sec["missing"]), sec["missing"])

        st, _, _, j = cl.req("POST", "/folders-ui/scenario",
                             {"key": "17", "scenario": "without-north"}, origin=me)
        keys = [s["key"] for s in j["state"]["sections"]]
        check("the scenario was created as a copy and is visible to both halves",
              st == 200 and "17:without-north" in keys
              and "17:without-north" in folders.load(ini), keys)

        st, _, _, j = cl.req("POST", "/folders-ui/section",
                             {"dashboard_id": "42"}, origin=me)
        check("a new report was added as an empty section",
              st == 200 and "42" in [s["key"] for s in j["state"]["sections"]], j)
        empty = [s for s in j["state"]["sections"] if s["key"] == "42"][0]
        check("an empty section speaks of itself aloud and does not show \"0 files\"",
              empty["files"] == 0 and empty["missing"], empty)

        st, _, _, j = cl.req("POST", "/folders-ui/entry/remove",
                             {"key": "17", "index": 1}, origin=me)
        sec = [s for s in j["state"]["sections"] if s["key"] == "17"][0]
        check("the entry was removed from the display by index",
              st == 200 and len(sec["entries"]) == 1, sec["entries"])

        st, _, _, j = cl.req("POST", "/folders-ui/check", {"key": "17"}, origin=me)
        check("\"Check\" counts the named section and does not touch the file",
              st == 200 and [s for s in j["sections"] if s["key"] == "17"][0]["checked"],
              j["sections"])

        # ---------------------------------------------------------------- #
        # 4. A replacement for the system folder-picker dialog.
        # ---------------------------------------------------------------- #
        st, _, _, j = cl.req("POST", "/folders-ui/probe", {"path": str(d1)},
                             origin=me)
        check("probe sees the folder and lists what is in it",
              st == 200 and j["state"] == "dir" and j["count"] == 2
              and "sl.xlsx" in j["names"], j)
        check("probe gives the same root_id that will go to the platform",
              j["root_id"] == folders.identity(str(d1)), j.get("root_id"))
        _, _, _, j = cl.req("POST", "/folders-ui/probe",
                            {"path": str(tmp / "no-such-thing")}, origin=me)
        check("probe honestly says \"nothing there\" and does not stay silent",
              j["state"] == "missing", j)
        _, _, _, j = cl.req("POST", "/folders-ui/probe",
                            {"path": str(d1 / "sl.xlsx")}, origin=me)
        check("probe tells a file from a folder", j["state"] == "file", j)
        _, _, _, j = cl.req("POST", "/folders-ui/probe", {"path": ""}, origin=me)
        check("an empty path is a refusal with a code, not \"nothing there\"",
              j["ok"] is False and j["error"] == "bad_path", j)

        # ---------------------------------------------------------------- #
        # 5. The boundary: Host, Origin, body, methods.
        # ---------------------------------------------------------------- #
        st, _, _, _ = cl.req("GET", "/folders-ui/state.json",
                             host="attacker.invalid")
        check("a foreign Host is a refusal (DNS rebinding)", st == 421, st)
        st, _, _, j = cl.req("POST", "/folders-ui/entry/add",
                             {"key": "17", "kind": "dir", "raw": str(d2)},
                             origin="https://onepage.example.com")
        check("a foreign Origin does not write to the settings file, even the platform origin",
              st == 403 and j["error"] == "bad_origin", (st, j))
        st, hd, _, _ = cl.req("OPTIONS", "/folders-ui/entry/add",
                              origin="https://evil.invalid")
        check("the preflight does not pass: 405 and not a single CORS header",
              st == 405 and not any(k.lower().startswith("access-control")
                                    for k in hd), (st, hd))
        st, hd, body, _ = cl.req("HEAD", "/folders-ui/state.json")
        check("HEAD answers with headers and no body", st == 200 and not body, st)
        st, _, _, j = cl.req("POST", "/folders-ui/entry/add",
                             {"key": "17", "kind": "dir", "raw": "a\nb"},
                             origin=me)
        check("a bad path is a 400 with a code and a fresh state",
              st == 400 and j["error"] == "bad_path" and "state" in j, j)
        st, _, _, j = cl.req("POST", "/folders-ui/entry/remove",
                             {"key": "999", "index": 0}, origin=me)
        check("an edit of a non-existent section is a refusal by code",
              st == 400 and j["error"] == "no_section", j)
        st, _, _, _ = cl.req("POST", "/folders-ui/nope", {}, origin=me)
        check("an unknown action is a 404", st == 404, st)

        before = ini.read_bytes()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", "/folders-ui/entry/add",
                  body=b"x" * (foldersui.MAX_BODY + 10),
                  headers={"Content-Type": "application/json", "Origin": me})
        st = c.getresponse().status
        check("a body over the limit is rejected, the file is untouched",
              st == 400 and ini.read_bytes() == before, st)

        # ---------------------------------------------------------------- #
        # 6. A broken file: the screen opens and names the reason.
        # ---------------------------------------------------------------- #
        ini.write_text("just text\n", encoding="utf-8")
        _, _, _, j = cl.req("GET", "/folders-ui/state.json")
        check("an unfit file: the screen opens and says why",
              j["problem"] == "unparsable" and j["writable"] is False
              and j["comments_preserved"] is False, j)
        ini.write_text("[folders:5]\ndir = A\n\n[folders:5]\ndir = B\n",
                       encoding="utf-8")
        _, _, _, j = cl.req("GET", "/folders-ui/state.json")
        check("a duplicate header is named in advance, not by a refusal on the button",
              j["problem"] == "duplicate_section" and j["duplicate_keys"] == ["5"],
              j)
        st, _, _, j = cl.req("POST", "/folders-ui/entry/add",
                             {"key": "5", "kind": "dir", "raw": str(d2)},
                             origin=me)
        check("and an edit of such a file really refuses",
              st == 400 and j["error"] == "duplicate_section", j)

        # ---------------------------------------------------------------- #
        # 7. A path is a personal thing: it does not go to the log.
        # ---------------------------------------------------------------- #
        # We check the CODE, not the docstring: a mention of the prohibition in
        # the module header is a promise, and only a line of code can break it.
        import ast
        src = Path(foldersui.__file__).read_text("utf-8")
        calls = {n.func.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
        check("the screen code has neither print nor applog: a path does not go to the log",
              "print" not in calls and not any("applog" in x for x in names),
              sorted(calls))
        # ── 9. Entry points. A screen that a person cannot reach, ──
        # is equal to an unwritten one: `appwindow.open_folders` was once
        # already written and called from nowhere, and the kit would have gone
        # out with a screen known only to someone who remembers the address
        # /folders-ui/.
        from agent import controller as _ctl, tray as _tray
        check("the controller has open_folders", callable(getattr(_ctl.AgentController, "open_folders", None)))
        src_ctl = (ROOT / "agent" / "controller.py").read_text("utf-8")
        check("the controller calls exactly appwindow.open_folders",
              "appwindow.open_folders(" in src_ctl)
        menu = _tray.TrayState().menu()
        check("the \"Report folders…\" item is in the icon menu",
              any(c == _tray.CMD_FOLDERS for c, _, _ in menu), menu)
        check("the item is always available (not under the mirror)",
              all(en for c, _, en in menu if c == _tray.CMD_FOLDERS))
        src_tray = (ROOT / "agent" / "tray.py").read_text("utf-8")
        check("the icon routes CMD_FOLDERS to the controller",
              "cmd == CMD_FOLDERS" in src_tray and "self.c.open_folders()" in src_tray)
        bat = (ROOT / "deploy" / "agent_tools" / "op-agent.bat").read_text("utf-8")
        check("op-agent.bat understands the folders command", '"%~1"=="folders"' in bat)
        check("the folders branch calls open_folders, not the library",
              "open_folders(n" in bat)
        check("folders.ini.example goes into the kit",
              "folders.ini.example" in (ROOT / "deploy" / "make-agent-kit.sh").read_text("utf-8"))

        # ── 10. Choosing a path with the mouse. "Folders are declared by text" is ──
        # exactly the place where a person stumbled: they saw the run's refusal
        # and reasonably asked why a folder cannot be chosen with the mouse.
        # The dialog exists only on Windows, so what is checked here is the
        # CONTRACT: the handler answers, does not crash on macOS, and the
        # button reaches it.
        st, _, _, j = cl.req("POST", "/folders-ui/pick", {"kind": "dir"}, origin=me)
        check("the path-pick handler answers 200", st == 200, st)
        check("outside Windows the pick is honestly empty, not an error",
              isinstance(j, dict) and j.get("ok") is False and j.get("path") == "", j)
        check("a foreign origin does not reach the dialog",
              cl.req("POST", "/folders-ui/pick", {"kind": "dir"},
                     origin="https://evil.example")[0] == 403)
        src_js = (ROOT / "agent" / "web" / "folders.js").read_text("utf-8")
        check("the screen has the \"Browse…\" button", "add_browse" in src_js)
        check("the button calls exactly the pick handler", "post('pick'" in src_js)
        check("the chosen path is NOT written to the config by itself but lands in the field",
              "path.value = j.path" in src_js)
        src_aw = (ROOT / "agent" / "appwindow.py").read_text("utf-8")
        check("the dialog asks the shell for a file-system path",
              "_SIGDN_FILESYSPATH" in src_aw and "FOS_PICKFOLDERS" in src_aw)
        check("COM is switched off only if we ourselves switched it on",
              "owns_com" in src_aw)
        # The run's refusal must name the road to the screen, not only the file:
        # "declare the data folders" sent the person looking for what to declare with.
        src_runs = (ROOT / "agent" / "runs.py").read_text("utf-8")
        check("the \"no section\" refusal leads to the folders screen",
              "Report folders" in src_runs and "Browse" in src_runs)

        # ── 11. The S3 bucket panel. WinSCP fields, a write-only secret, ──
        # a real connection test. The BOUNDARY is checked: what goes into the
        # file, what goes NOWHERE, and that the handler's response carries no secret.
        SECRET = "wJalrXUtnFEMI-NOT-A-REAL-KEY"
        WIN = {"name": "ldwh", "host": "storage.yandexcloud.net", "port": 443,
               "tls": True, "access_key": "YCAJE-access-id",
               "remote": "prod-s3-nwd", "target": str(d2),
               "region": "ru-central1", "path_style": True,
               "include": "*.xlsx", "exclude": "~$*"}

        st, _, _, j = cl.req("POST", "/folders-ui/s3/preview", WIN, origin=me)
        check("the parse of the WinSCP fields is named aloud before saving",
              st == 200 and j["bucket"] == "prod-s3-nwd" and j["prefix"] == ""
              and j["endpoint"] == "https://storage.yandexcloud.net", j)
        _, _, _, j = cl.req("POST", "/folders-ui/s3/preview",
                            dict(WIN, remote="/prod-s3-nwd//exports_été//"),
                            origin=me)
        check("\"bucket/subfolder\" and extra slashes are parsed predictably",
              j["bucket"] == "prod-s3-nwd" and j["prefix"] == "exports_été/", j)
        _, _, _, j = cl.req("POST", "/folders-ui/s3/preview",
                            dict(WIN, host="storage.yandexcloud.net:443"),
                            origin=me)
        check("a port inside the address is a refusal with an explanation, not a double port",
              j["ok"] is False and j["error"] == "bad_s3_host_port", j)
        _, _, _, j = cl.req("POST", "/folders-ui/s3/preview",
                            dict(WIN, remote="/"), origin=me)
        check("an empty Remote directory is a refusal, not a bucket without a name",
              j["ok"] is False and j["error"] == "bad_s3_remote", j)

        st, _, _, j = cl.req("POST", "/folders-ui/s3/save",
                             dict(WIN, secret=SECRET), origin=me)
        saved_s3 = j["state"]["s3"]["sources"]
        check("the connection is saved and visible on the screen",
              st == 200 and len(saved_s3) == 1
              and saved_s3[0]["name"] == "ldwh", j)
        check("the screen shows the fields in the same form in which they were entered",
              saved_s3[0]["host"] == "storage.yandexcloud.net"
              and saved_s3[0]["port"] == 443 and saved_s3[0]["tls"] is True
              and saved_s3[0]["remote"] == "prod-s3-nwd", saved_s3[0])
        ini_text = agent_ini.read_text("utf-8")
        check("the secret is in agent.ini in no form", SECRET not in ini_text,
              ini_text)
        check("only the non-secret went into agent.ini",
              "[s3:ldwh]" in ini_text
              and "endpoint = https://storage.yandexcloud.net" in ini_text
              and "bucket = prod-s3-nwd" in ini_text
              and "access_key = YCAJE-access-id" in ini_text, ini_text)
        check("a foreign agent.ini section is untouched",
              "[security]" in ini_text and "allowed_origins" in ini_text)
        check("the secret does not come back out, only a flag",
              SECRET not in json.dumps(j, ensure_ascii=False)
              and saved_s3[0]["secret_set"] is True, saved_s3[0])
        # The run will read the same thing: the display and `s3-sync` must not diverge.
        from agent import s3sync as _s3
        loaded = _s3.load_s3(agent_ini)
        check("the run will read exactly what the screen shows",
              "ldwh" in loaded and loaded["ldwh"].bucket == "prod-s3-nwd"
              and loaded["ldwh"].prefix == ""
              and loaded["ldwh"].endpoint == "https://storage.yandexcloud.net",
              str(loaded))
        check("the credential entry name is the same as for the kit commands",
              loaded["ldwh"].cred_name == "s3-ldwh")

        st, _, _, j = cl.req("POST", "/folders-ui/s3/save",
                             dict(WIN, remote="prod-s3-nwd/exports_été",
                                  region="ru-central1"), origin=me)
        after = j["state"]["s3"]["sources"][0]
        check("an edit without a secret does not lose the saved secret",
              after["secret_set"] is True and after["prefix"] == "exports_été/",
              after)

        st, _, _, j = cl.req("POST", "/folders-ui/s3/save",
                             dict(WIN, secret=SECRET),
                             origin="https://onepage.example.com")
        check("a foreign Origin does not save the bucket and does not touch the secret",
              st == 403 and j["error"] == "bad_origin", (st, j))
        # The check goes by WHAT IS ON THE SCREEN, and before saving: a person
        # presses it precisely so as not to save blindly. The former refusal
        # "save first" made the button a trap: it is there, visible, and on a
        # new connection always answered "no".
        st, _, raw0, j = cl.req(
            "POST", "/folders-ui/s3/test",
            dict(WIN, name="not-saved-yet", host="127.0.0.1", port=9, tls=False,
                 secret=SECRET),
            origin=me)
        text0 = raw0.decode("utf-8", "replace")
        check("the check works on an UNsaved connection",
              st == 200 and j.get("ok") is False and j.get("error") == "s3_failed",
              (st, text0[:160]))
        check("checking an unsaved one does not require saving",
              "save the connection" not in text0.lower(), text0[:160])
        check("the typed secret does not come back",
              SECRET not in text0, text0[:160])
        check("the unsaved connection was not written to agent.ini",
              "[s3:not-saved-yet]" not in agent_ini.read_text("utf-8"))

        check("the connection check is unavailable to a foreign Origin",
              cl.req("POST", "/folders-ui/s3/test", {"name": "ldwh"},
                     origin="https://evil.example")[0] == 403)
        st_bad, _, _, j_bad = cl.req("POST", "/folders-ui/s3/pull",
                                     {"name": "ldwh"},
                                     origin="https://evil.example")
        check("\"Download now\" is unavailable to a foreign Origin",
              st_bad == 403 and j_bad["error"] == "bad_origin", (st_bad, j_bad))

        # The connection check is real: the address is deliberately dead, and the
        # refusal must be a phrase, not a traceback.
        cl.req("POST", "/folders-ui/s3/save",
               dict(WIN, host="127.0.0.1", port=9, tls=False, secret=SECRET),
               origin=me)
        st, _, raw, j = cl.req("POST", "/folders-ui/s3/test", {"name": "ldwh"},
                               origin=me)
        check("a check refusal is human text, not a traceback",
              st == 200 and j["ok"] is False and j["error"] == "s3_failed"
              and "Traceback" not in j.get("detail", "")
              and "storage" in j.get("detail", ""), j)
        check("neither the secret nor the key in the refusal response",
              SECRET not in raw.decode("utf-8", "replace"), j)

        # "Download now" is the same road as the run's, and the same address,
        # deliberately dead. The response comes AT ONCE ("started"), and the
        # result is fetched by polling: a request hanging for a minute of
        # download is indistinguishable from a hung program, and that is exactly
        # why the handler is built the same as "Update now" in the report
        # mirror.
        st, _, raw2, j = cl.req("POST", "/folders-ui/s3/pull", {"name": "ldwh"},
                                origin=me)
        check("download now: the handler answers at once, not holding the connection",
              st == 200 and j.get("ok") is True and j.get("started") is True,
              (st, j))
        snap = {}
        for _ in range(100):
            _, _, raw3, prog = cl.req("GET", "/folders-ui/s3/progress")
            snap = ((prog or {}).get("pulls") or {}).get("ldwh") or {}
            if snap and snap.get("running") is False:
                break
            time.sleep(0.05)
        text2 = raw3.decode("utf-8", "replace")
        check("download now: progress and result arrive by polling",
              snap.get("running") is False, str(snap)[:200])
        check("download now: a refusal is human text, not a traceback",
              snap.get("ok") is False and "Traceback" not in text2
              and "urllib" not in text2, str(snap)[:200])
        check("download now: neither the secret nor the key in the response",
              SECRET not in text2 and "YCAJE-access-id" not in text2, text2[:160])

        _, _, _, j = cl.req("POST", "/folders-ui/s3/forget", {"name": "ldwh"},
                            origin=me)
        check("the secret is forgotten: the flag is cleared, the connection remains",
              j["state"]["s3"]["sources"][0]["secret_set"] is False, j)
        _, _, _, j = cl.req("POST", "/folders-ui/s3/test", {"name": "ldwh"},
                            origin=me)
        check("without a secret the check says so and does not go to the network",
              j["ok"] is False and j["error"] == "no_s3_secret", j)

        _, _, _, j = cl.req("POST", "/folders-ui/s3/remove", {"name": "ldwh"},
                            origin=me)
        check("the connection was removed from agent.ini",
              j["state"]["s3"]["sources"] == []
              and "[s3:ldwh]" not in agent_ini.read_text("utf-8"), j)
        check("a foreign section survived the deletion",
              "[security]" in agent_ini.read_text("utf-8"))
        _, _, _, j = cl.req("POST", "/folders-ui/s3/test", {"name": "ldwh"},
                            origin=me)
        check("checking a non-existent connection is a refusal by code",
              j["error"] == "no_section", j)
        _, _, _, j = cl.req("POST", "/folders-ui/s3/save",
                            dict(WIN, name="bad name"), origin=me)
        check("a bad connection name is a 400 with a code", j["error"] == "bad_s3_name", j)

        # The same prohibition as for the folders screen: what arrives here is a
        # folder path and bucket object names, which the person sees on their
        # own screen and nobody else.
        from agent import s3ui as _s3ui
        src_s3 = Path(_s3ui.__file__).read_text("utf-8")
        tree = ast.parse(src_s3)
        calls_s3 = {n.func.id for n in ast.walk(tree)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        names_s3 = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        check("the bucket panel has neither print nor applog",
              "print" not in calls_s3
              and not any("applog" in x for x in names_s3),
              sorted(calls_s3))
        src_js2 = (ROOT / "agent" / "web" / "folders.js").read_text("utf-8")
        check("the target folder is chosen with the same \"Browse…\" button",
              "s3_target" in src_js2 and "post('pick'" in src_js2)
        check("the screen has a button for the real check",
              "post('s3/test'" in src_js2)
        check("the parse of Remote directory is drawn from the agent\'s response, not by its own code",
              "post('s3/preview'" in src_js2 and "s3_parsed_remote" in src_js2)
        check("the screen has the \"Download now\" button and progress polling",
              "post('s3/pull'" in src_js2 and "s3/progress" in src_js2, "")
        # Every string the page asks the catalog for must be there: otherwise a
        # key name will appear on screen instead of a phrase, and a person will
        # notice it and not a test.
        strings = json.loads((ROOT / "agent" / "web" / "strings.json")
                             .read_text("utf-8"))
        # Keys built from pieces (`fmt('reason_' + m.reason)`) end with an
        # underscore: they are checked not by this test but by the `reason_*`
        # lines in the catalog itself.
        keys = {k for k in re.findall(r"fmt\('([a-z0-9_]+)'", src_js2)
                if not k.endswith("_")}
        missing = sorted(k for k in keys if k not in strings)
        check("all keys of the page are in strings.json", not missing, str(missing))

    finally:
        srv.shutdown()
        srv.server_close()
        if saved is None:
            os.environ.pop("OP_FOLDERS_INI", None)
        else:
            os.environ["OP_FOLDERS_INI"] = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'ALL PASS' if _FAIL == 0 else 'FAILURES: ' + str(_FAIL)}  ({_PASS} passed)")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
