"""Smoke test for agent/s3sync.py: SigV4 signature, listing, download, a pass.

Run:  python -m agent.tests.smoke_s3sync   (from the OnePage repo root)

The main check of this file is the SIGNATURE. It cannot be checked "by eye" and
cannot be checked with our own code: a wrong signature looks like a 403 with no
explanation, and from the storage's reply it is impossible to tell "wrong key"
from "we compute it wrong". So the four reference signatures below were
compared with botocore (`botocore.auth.S3SigV4Auth`, version 1.42.97,
14.10.2026) and frozen as golden values; if botocore is on the machine, the
test compares against it AGAIN, and if not, the frozen values apply. botocore
is not in the kit and will not be (offline install, four wheels).

The rest runs against a fake storage on `ThreadingHTTPServer`: it parses
`Authorization`, serves `ListObjectsV2` XML in pages and the objects
themselves. The test needs no outside network.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent import s3sync, s3ui

_PASS = 0
_FAIL = 0
_TMP = Path(tempfile.mkdtemp(prefix="op-s3-test-"))


def check(label, cond, extra=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"PASS {label}")
    else:
        _FAIL += 1
        print(f"FAIL {label}  {extra}")


AK = "AKIAIOSFODNN7EXAMPLE"
SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
FIXED = time.strptime("20260214T091530Z", "%Y%m%dT%H%M%SZ")

# (url, params, region) -> signature. Checked against botocore.auth.S3SigV4Auth.
GOLDEN = [
    ("https://s3.example.com/bucket/",
     {"list-type": "2", "prefix": "sales/weekly/", "max-keys": "1000"},
     "eu-west-1",
     "69c1ef1bfeec0b4698ec79078b0278e0dd5b97c4216affdcc57ffcd943db22cd"),
    ("https://s3.example.com/bucket/sales/weekly/"
     "%D0%BE%D1%82%D1%87%D1%91%D1%82%20%281%29.xlsx", {}, "us-east-1",
     "447a432232cb65e59f8b3b598840627969c128e3e7496907e027aa16bc60bd7e"),
    ("https://bucket.s3.example.com/a%2Bb%20c/%7Etilde.xlsx", {}, "ru-central1",
     "b72fac1d6da11dae5981c1eed2b7d080e5069f94d09a15c1c9d9252cc7b6727a"),
    ("https://s3.example.com:9000/bucket/",
     {"list-type": "2", "continuation-token": "1/abc+def=="}, "us-east-1",
     "289f185f18a2486121775a60be5bbc222772a8906a25a1929923e3f91e452a49"),
]


# --------------------------------------------------------------------------- #
# 1. Signature                                                                #
# --------------------------------------------------------------------------- #

def test_signature():
    for url, params, region, want in GOLDEN:
        h = s3sync.sign_headers("GET", url, params, access_key=AK, secret=SK,
                                region=region, now=FIXED)
        got = h["Authorization"].rsplit("Signature=", 1)[1]
        check(f"signature matches the reference: {url[:48]}…", got == want,
              f"{got}\n     != {want}")
    h = s3sync.sign_headers("GET", GOLDEN[0][0], GOLDEN[0][1], access_key=AK,
                            secret=SK, region="eu-west-1", now=FIXED)
    check("the headers do not contain the secret itself", SK not in json.dumps(h), repr(h))
    check("exactly host, body sha and date are signed",
          "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in h["Authorization"])
    check("the body is empty and that is stated honestly",
          h["x-amz-content-sha256"] == s3sync.EMPTY_SHA256)
    # The region is part of the key's scope: a mistake in it is the most common
    # "403 for no reason", so its participation is pinned separately.
    other = s3sync.sign_headers("GET", GOLDEN[0][0], GOLDEN[0][1], access_key=AK,
                                secret=SK, region="us-east-1", now=FIXED)
    check("another region gives another signature",
          other["Authorization"] != h["Authorization"])
    # The canonical request is sensitive to the sorting of parameters.
    check("query parameters are sorted and encoded",
          s3sync.canonical_query({"b": "2", "a": "1/x+y"}) == "a=1%2Fx%2By&b=2",
          s3sync.canonical_query({"b": "2", "a": "1/x+y"}))


def test_signature_against_botocore():
    try:
        from botocore.auth import S3SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
        import botocore.auth as ba
        import datetime
    except Exception:                                        # noqa: BLE001
        print("SKIP botocore is not installed -- the check runs against the frozen "
              "references above (botocore is not in the kit and never will be)")
        return
    ba.get_current_datetime = lambda: datetime.datetime(
        2026, 2, 14, 9, 15, 30, tzinfo=datetime.timezone.utc)
    bad = []
    for url, params, region, _want in GOLDEN:
        ours = s3sync.sign_headers("GET", url, params, access_key=AK, secret=SK,
                                   region=region, now=FIXED)["Authorization"]
        full = url + ("?" + s3sync.canonical_query(params) if params else "")
        req = AWSRequest(method="GET", url=full,
                         headers={"x-amz-content-sha256": s3sync.EMPTY_SHA256})
        S3SigV4Auth(Credentials(AK, SK), "s3", region).add_auth(req)
        if req.headers["Authorization"] != ours:
            bad.append(url)
    check("live comparison with botocore: all four requests signed identically",
          not bad, ", ".join(bad))


# --------------------------------------------------------------------------- #
# 2. Fake storage                                                             #
# --------------------------------------------------------------------------- #

class Bucket:
    """Contents of the fake bucket: key -> bytes."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.requests: list[str] = []
        self.deny = False            # answer 403 to everything
        self.short = set()           # keys whose response is cut off
        self.page = 2                # objects per listing page

    def etag(self, key: str) -> str:
        import hashlib
        return hashlib.md5(self.objects[key]).hexdigest()      # noqa: S324


def _xml(bucket: Bucket, prefix: str, token: str) -> bytes:
    keys = sorted(k for k in bucket.objects if k.startswith(prefix))
    start = int(token) if token.isdigit() else 0
    page = keys[start:start + bucket.page]
    nxt = start + bucket.page
    truncated = nxt < len(keys)
    items = "".join(
        f"<Contents><Key>{k}</Key><Size>{len(bucket.objects[k])}</Size>"
        f"<ETag>&quot;{bucket.etag(k)}&quot;</ETag>"
        f"<LastModified>2026-02-14T09:15:30.000Z</LastModified></Contents>"
        for k in page)
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f'<Name>bkt</Name><Prefix>{prefix}</Prefix>'
            f'<IsTruncated>{"true" if truncated else "false"}</IsTruncated>'
            f'{"<NextContinuationToken>%d</NextContinuationToken>" % nxt if truncated else ""}'
            f'{items}</ListBucketResult>').encode("utf-8")


def start_bucket(bucket: Bucket):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):            # silence in the test output
            pass

        def _fail(self, code: int, s3code: str):
            body = (f'<?xml version="1.0"?><Error><Code>{s3code}</Code>'
                    f'<Message>no</Message></Error>').encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                                      # noqa: N802
            bucket.requests.append(self.path)
            auth = self.headers.get("Authorization") or ""
            if not auth.startswith("AWS4-HMAC-SHA256 Credential=") or \
                    "Signature=" not in auth or \
                    self.headers.get("x-amz-content-sha256") != s3sync.EMPTY_SHA256:
                return self._fail(403, "SignatureDoesNotMatch")
            if bucket.deny:
                return self._fail(403, "AccessDenied")
            parts = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(parts.query)
            path = urllib.parse.unquote(parts.path)
            if q.get("list-type") == ["2"]:
                body = _xml(bucket, (q.get("prefix") or [""])[0],
                            (q.get("continuation-token") or [""])[0])
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            key = path.split("/bkt/", 1)[1] if "/bkt/" in path else path.lstrip("/")
            if key not in bucket.objects:
                return self._fail(404, "NoSuchKey")
            data = bucket.objects[key]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            # Cut-off: we promise a length and send half -- exactly what
            # `read(n)` stays silent about, and the file lands truncated.
            self.wfile.write(data[: len(data) // 2] if key in bucket.short else data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def source(endpoint: str, target: Path, **kw) -> s3sync.S3Source:
    opts = dict(name="test", endpoint=endpoint, bucket="bkt", access_key=AK,
                target=str(target), region="us-east-1", prefix="sales/",
                path_style=True)
    opts.update(kw)
    return s3sync.S3Source(**opts)


# --------------------------------------------------------------------------- #
# 3. A whole pass                                                             #
# --------------------------------------------------------------------------- #

def test_sync():
    b = Bucket()
    b.objects["sales/week01.xlsx"] = b"A" * 1000
    b.objects["sales/week02.xlsx"] = b"B" * 2000
    b.objects["sales/week03.xlsx"] = b"C" * 3000
    b.objects["sales/readme.txt"] = b"not a workbook"
    b.objects["sales/sub/week04.xlsx"] = b"D" * 400
    srv, base = start_bucket(b)
    root = _TMP / "sync"
    target = root / "data"
    state = root / "state"
    src = source(base, target, include=("*.xlsx",))
    seen: list[tuple] = []
    try:
        rep = s3sync.sync(src, SK, state_root=state,
                          progress=lambda d, t, n: seen.append((d, t, n)))
        check("the pass finished without errors", rep.ok, repr(rep.errors))
        check("listing pages are glued together (4 workbooks out of 5 objects)",
              rep.matched == 4, f"{rep.matched} / listed={rep.listed}")
        check("the listing went in pages (more than one request)",
              len([r for r in b.requests if "list-type" in r]) >= 2,
              str(b.requests))
        check("four files downloaded", rep.downloaded == 4, str(rep.downloaded))
        check("bytes are counted", rep.bytes_downloaded == 6400,
              str(rep.bytes_downloaded))
        check("the mask cut off the non-workbook",
              not (target / "readme.txt").exists())
        check("the key's subfolder is preserved",
              (target / "sub" / "week04.xlsx").read_bytes() == b"D" * 400)
        check("content matches byte for byte",
              (target / "week02.xlsx").read_bytes() == b"B" * 2000)
        check("no temporary files left",
              not list(target.rglob("*.op-s3-tmp")))
        check("progress was reported and reached the end",
              seen and seen[-1][0] == seen[-1][1] == 4, repr(seen[-3:]))

        # Second pass: nothing changed -- do not download again.
        b.requests.clear()
        rep2 = s3sync.sync(src, SK, state_root=state)
        check("the second pass downloaded nothing",
              rep2.downloaded == 0 and rep2.skipped == 4,
              f"downloaded={rep2.downloaded} skipped={rep2.skipped}")

        # An object changed -- download only it.
        b.objects["sales/week02.xlsx"] = b"B" * 2500
        rep3 = s3sync.sync(src, SK, state_root=state)
        check("the changed file is downloaded again, the others are not",
              rep3.downloaded == 1 and rep3.skipped == 3,
              f"downloaded={rep3.downloaded} skipped={rep3.skipped}")
        check("the new version is on disk",
              (target / "week02.xlsx").read_bytes() == b"B" * 2500)

        # Gone from the bucket -- remove OUR file, do not touch a foreign one.
        (target / "mío.xlsx").write_text("manual export", encoding="utf-8")
        del b.objects["sales/week03.xlsx"]
        rep4 = s3sync.sync(src, SK, state_root=state)
        check("a file that vanished from the bucket is deleted locally",
              not (target / "week03.xlsx").exists() and rep4.deleted == 1,
              str(rep4.deleted))
        check("a foreign file in the folder is not deleted",
              (target / "mío.xlsx").exists())
        check("a foreign file is named in the report", "mío.xlsx" in rep4.foreign,
              str(rep4.foreign))
    finally:
        srv.shutdown()


def test_safety_and_errors():
    b = Bucket()
    b.objects["sales/good.xlsx"] = b"ok" * 10
    b.objects["sales/../../escape.xlsx"] = b"zip-slip"
    b.objects["sales/short.xlsx"] = b"X" * 100
    b.short.add("sales/short.xlsx")
    srv, base = start_bucket(b)
    root = _TMP / "safety"
    target = root / "data"
    src = source(base, target, include=("*.xlsx",))
    try:
        rep = s3sync.sync(src, SK, state_root=root / "state")
        escaped = (root.parent / "escape.xlsx").exists() or \
                  (target.parent / "escape.xlsx").exists()
        check("a key with ../ is not written anywhere", not escaped)
        check("the refusal by name is stated out loud",
              any("unsafe" in msg for _k, msg in rep.errors), repr(rep.errors))
        check("a cut-off download left no file",
              not (target / "short.xlsx").exists())
        check("the cut-off is reported as an error, not as success",
              any("short.xlsx" in k for k, _ in rep.errors), repr(rep.errors))
        check("the whole file is still downloaded (one refusal does not sink the pass)",
              (target / "good.xlsx").is_file())

        b.deny = True
        rep2 = s3sync.sync(src, SK, state_root=root / "state2")
        text = " ".join(m for _k, m in rep2.errors)
        check("403 is explained to a human", "did not accept the key" in text, text)
        check("the refusal names the command for the secret", "set-cred s3-test" in text, text)
        check("the secret did not get into the refusal text", SK not in text)
        check("the access key did not get into the refusal text", AK not in text)
    finally:
        srv.shutdown()


def test_stop():
    b = Bucket()
    for i in range(6):
        b.objects[f"sales/w{i}.xlsx"] = b"Z" * 100
    srv, base = start_bucket(b)
    root = _TMP / "stop"
    src = source(base, root / "data", include=("*.xlsx",))
    ev = threading.Event()
    ev.set()                       # "Exit" pressed before the start
    try:
        rep = s3sync.sync(src, SK, state_root=root / "state", stop=ev)
        check("the stop is visible in the result", rep.stopped, repr(rep))
        check("a stopped pass downloaded nothing", rep.downloaded == 0)
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- #
# 4. Config                                                                   #
# --------------------------------------------------------------------------- #

def test_config():
    ini = _TMP / "agent.ini"
    ini.write_text(
        "[security]\nallowed_origins = https://x\n\n"
        "[s3:ldwh]\n"
        "endpoint = https://s3.company.local\n"
        "bucket = reports\n"
        "prefix = /sales/weekly/\n"
        "access_key = AKIA123\n"
        "target = C:\\OnePage\\sales\n"
        "region = ru-central1\n"
        "path_style = false\n"
        "include = *.xlsx, *.xlsb\n"
        "exclude = ~$*\n\n"
        "[s3:broken]\n"
        "bucket = nope\n\n"
        "[s3:scheme]\n"
        "endpoint = s3.company.local\nbucket = b\naccess_key = k\ntarget = t\n",
        encoding="utf-8")
    got = s3sync.load_s3(ini)
    check("the section was read", "ldwh" in got, str(list(got)))
    src = got.get("ldwh")
    check("a section without required fields is skipped, not guessed",
          "broken" not in got)
    check("an endpoint without a scheme is rejected", "scheme" not in got)
    if src:
        check("the prefix is normalized without a leading slash",
              src.prefix == "sales/weekly/", src.prefix)
        check("path_style = false was read", src.path_style is False)
        check("masks are parsed", src.include == ("*.xlsx", "*.xlsb"), str(src.include))
        check("the credential entry name has no colon (otherwise a file name on Windows)",
              src.cred_name == "s3-ldwh" and ":" not in src.cred_name)
    check("no file -- an empty dict, not a failure",
          s3sync.load_s3(_TMP / "absent.ini") == {})
    # Masks are the same as for folders: one answer to "what gets read".
    check("a mask works on the NAME, not on the path",
          s3sync.wanted(source("https://x", _TMP, include=("*.xlsx",)),
                        "sub/week01.xlsx"))
    check("exclusion beats inclusion",
          not s3sync.wanted(source("https://x", _TMP, include=("*.xlsx",),
                                   exclude=("~$*",)), "~$week01.xlsx"))
    check("unsafe keys are rejected by name",
          s3sync.relative_name("sales/../x.xlsx", "sales/") is None
          and s3sync.relative_name("sales/a\\b.xlsx", "sales/") is None
          and s3sync.relative_name("sales/C:x.xlsx", "sales/") is None
          and s3sync.relative_name("sales/ok.xlsx", "sales/") == "ok.xlsx")
    check("a placeholder folder (key ending in /) is not a file",
          s3sync.relative_name("sales/sub/", "sales/") is None)




# --------------------------------------------------------------------------- #
# 5. WinSCP fields -> [s3:...] section (agent/s3ui.py)                        #
# --------------------------------------------------------------------------- #

def test_winscp_mapping():
    """Parsing of what the person sees in the WinSCP window.

    The main thing here is `Remote directory`. In WinSCP the first segment of
    this field is the BUCKET, not a folder inside the bucket, and a parsing
    mistake is invisible: the listing simply returns zero objects. So the
    parsing is pinned with every form in which a person will write it.
    """
    cases = [
        ("prod-s3-nwd", ("prod-s3-nwd", "")),
        ("/prod-s3-nwd/", ("prod-s3-nwd", "")),
        ("prod-s3-nwd/exports_été", ("prod-s3-nwd", "exports_été/")),
        ("prod-s3-nwd/exports_été/", ("prod-s3-nwd", "exports_été/")),
        ("//prod-s3-nwd//a//b//", ("prod-s3-nwd", "a/b/")),
        ("prod-s3-nwd\\a\\b", ("prod-s3-nwd", "a/b/")),
    ]
    for raw, want in cases:
        got = s3ui.split_remote(raw)
        check(f"Remote directory {raw!r} -> bucket+prefix", got == want, got)
    for bad in ("", "   ", "/", "//", "../prod", "prod/../../x"):
        try:
            s3ui.split_remote(bad)
            check(f"invalid Remote directory {bad!r} rejected", False)
        except s3ui.IniError as e:
            check(f"invalid Remote directory {bad!r} rejected",
                  e.code == "bad_s3_remote", e.code)
    check("the prefix always ends with a slash: weekly does not match weekly-2025",
          s3ui.split_remote("b/weekly")[1] == "weekly/")
    check("bucket and prefix are assembled back into the WinSCP field",
          s3ui.join_remote("b", "a/") == "b/a"
          and s3ui.join_remote("b", "") == "b")

    # Host + Port + Encryption -> endpoint. The default port is not written
    # into the address: it is part of the signature, and a stray `:443` gives a 403 with no explanation.
    check("TLS + 443 -> https without a port",
          s3ui.make_endpoint("storage.yandexcloud.net", 443, True)
          == "https://storage.yandexcloud.net")
    check("a non-standard port stays in the address",
          s3ui.make_endpoint("minio.local", 9000, False)
          == "http://minio.local:9000")
    check("an empty port -- the default from the encryption",
          s3ui.make_endpoint("x.local", "", False) == "http://x.local")
    check("a pasted scheme is stripped, not doubled",
          s3ui.make_endpoint("https://x.local/", 443, True) == "https://x.local")
    for bad_host in ("x.local:443", "", "a b", "x.local/bucket"):
        try:
            s3ui.make_endpoint(bad_host, 443, True)
            check(f"invalid address {bad_host!r} rejected", False)
        except s3ui.IniError as e:
            check(f"invalid address {bad_host!r} rejected",
                  e.code.startswith("bad_s3_host"), e.code)
    check("the reverse parse returns the same fields",
          s3ui.split_endpoint("https://storage.yandexcloud.net")
          == {"host": "storage.yandexcloud.net", "port": 443, "tls": True})
    check("the reverse parse of a non-standard port",
          s3ui.split_endpoint("http://minio.local:9000")
          == {"host": "minio.local", "port": 9000, "tls": False})

    # Writing a section: the person's comments and other sections are left alone.
    text = ("; no password goes here\n[security]\nallowed_origins = https://x\n")
    name, values = s3ui.form_to_values({
        "name": "ldwh", "host": "storage.yandexcloud.net", "port": 443,
        "tls": True, "access_key": "YCAJE", "remote": "prod-s3-nwd/exports_été",
        "target": str(_TMP / "down"), "region": "ru-central1",
        "path_style": True, "include": "*.xlsx", "exclude": "~$*"})
    out = s3ui.write_section(text, name, values)
    check("the comment and the foreign section survived the write",
          "; no password goes here" in out and "allowed_origins = https://x" in out,
          out)
    ini = _TMP / "winscp.ini"
    ini.write_text(out, encoding="utf-8")
    loaded = s3sync.load_s3(ini)
    check("what was written is read by the same load_s3 as the run uses",
          "ldwh" in loaded and loaded["ldwh"].bucket == "prod-s3-nwd"
          and loaded["ldwh"].prefix == "exports_été/"
          and loaded["ldwh"].region == "ru-central1"
          and loaded["ldwh"].include == ("*.xlsx",), str(loaded))
    again = s3ui.write_section(out, "ldwh", dict(values, prefix="", include=""))
    ini.write_text(again, encoding="utf-8")
    loaded = s3sync.load_s3(ini)
    check("a repeated write fixes the lines instead of multiplying them",
          again.count("bucket =") == 1 and again.count("[s3:ldwh]") == 1
          and loaded["ldwh"].prefix == "" and loaded["ldwh"].include == (),
          again)
    check("deleting a section does not touch its neighbours",
          "[s3:ldwh]" not in s3ui.drop_section(again, "ldwh")
          and "[security]" in s3ui.drop_section(again, "ldwh"))


def test_connection_check():
    """The "Check connection" button really goes to the storage."""
    b = Bucket()
    b.objects["sales/week01.xlsx"] = b"A" * 1000
    b.objects["sales/week02.xlsx"] = b"B" * 2000
    b.objects["sales/readme.txt"] = b"x"
    srv, base = start_bucket(b)
    root = _TMP / "check"
    creds = root / "creds-root"
    ini = root / "agent.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)
    host = base[len("http://"):]
    ini.write_text(
        "[s3:probe]\n"
        f"endpoint = {base}\n"
        "bucket = bkt\nprefix = sales/\n"
        f"access_key = {AK}\n"
        f"target = {root / 'data'}\n"
        "include = *.xlsx\n", encoding="utf-8")
    saved = {k: os.environ.get(k) for k in
             ("OP_AGENT_INI", "OP_AGENT_DEV_CRED_STORE")}
    os.environ["OP_AGENT_INI"] = str(ini)
    os.environ["OP_AGENT_DEV_CRED_STORE"] = "1"
    try:
        check("the fake storage address parses into WinSCP fields",
              s3ui.split_endpoint(base)["host"] == host.split(":")[0])
        try:
            s3ui.test_connection({"name": "probe"}, cred_root=creds)
            check("without a secret the check does not go to the network", False)
        except s3ui.IniError as e:
            check("without a secret the check does not go to the network",
                  e.code == "no_s3_secret", e.code)
        s3ui.set_secret("probe", SK, cred_root=creds)
        res = s3ui.test_connection({"name": "probe"}, cred_root=creds)
        check("the check shows what is visible: how many, which, how much they weigh",
              res["ok"] and res["listed"] == 3 and res["matched"] == 2
              and res["bytes"] == 3000
              and res["names"] == ["week01.xlsx", "week02.xlsx"], res)
        check("the check downloaded nothing",
              not (root / "data").exists(), str(root))
        b.deny = True
        res = s3ui.test_connection({"name": "probe"}, cred_root=creds)
        check("a storage refusal is a phrase with an S3 code, not a traceback",
              res["ok"] is False and res["error"] == "s3_failed"
              and "AccessDenied" in res["detail"]
              and "Traceback" not in res["detail"], res)
        check("neither the secret nor the key in the refusal text",
              SK not in res["detail"] and AK not in res["detail"], res)
    finally:
        srv.shutdown()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_bucket_as_source():
    """The bucket is a SOURCE of the report, not a setting next to it.

    The owner: "I need a connection to a bucket, to read reports from there".
    The link is expressed by the FOLDER: what the report declared in
    `folders.ini` and where the connection from `agent.ini` downloads. Both the
    link itself (both sides of nesting) and what it exists for are checked: the
    run goes to the bucket BEFORE reading the folder, an unreachable bucket
    gives a NAMED refusal instead of a silent computation on yesterday's files,
    and an unchecked flag means "do not go to the network".
    """
    from agent import folders, runs

    def src_with(*entries):
        return folders.Source(
            dashboard_id="d1",
            entries=tuple(folders.Entry(raw=r, kind=k) for r, k in entries))

    def conn(name, target, refresh=True):
        return s3sync.S3Source(name=name, endpoint="https://s3.x", bucket="b",
                               access_key="AK", target=target,
                               refresh_before_run=refresh)

    base = str(_TMP / "Exports_été")
    inner = str(_TMP / "Exports_été" / "nwd")

    got = s3sync.connections_for(src_with((base, "dir")), {"a": conn("a", base)})
    check("link: the same folder -- connection found", [c.name for c in got] == ["a"], str(got))

    # The spelling must decide nothing: the comparison is by normalized
    # absolute path, not by strings (otherwise "the same bucket" would depend on
    # whether the person put a trailing slash or not).
    got = s3sync.connections_for(src_with((base + os.sep + ".", "dir")),
                                 {"a": conn("a", base + os.sep)})
    check("link: another spelling of the same folder -- found", len(got) == 1, str(got))

    got = s3sync.connections_for(src_with((base, "dir")), {"a": conn("a", inner)})
    check("link: target inside the report folder -- found", len(got) == 1, str(got))

    got = s3sync.connections_for(src_with((inner, "dir")), {"a": conn("a", base)})
    check("link: report folder inside the target -- also found", len(got) == 1, str(got))

    other = str(_TMP / "Other")
    got = s3sync.connections_for(src_with((base, "dir")), {"a": conn("a", other)})
    check("link: a foreign folder is not attracted", got == [], str(got))

    # A name neighbour is not nesting: `Exports_été-2026` is not inside `Exports_été`.
    got = s3sync.connections_for(src_with((base, "dir")),
                                 {"a": conn("a", base + "-2026")})
    check("link: a neighbour folder with the same name start is not attracted",
          got == [], str(got))

    got = s3sync.connections_for(src_with((inner + os.sep + "Ventes_été.xlsx", "file")),
                                 {"a": conn("a", inner)})
    check("link: a named file inside the target -- found", len(got) == 1, str(got))

    # -- the run ------------------------------------------------------------
    class FakeRun:
        def __init__(self):
            self.events = []

        def emit(self, ev):
            self.events.append(ev)

    class FakeStore:
        def __init__(self, secret="SEKRET"):
            self.secret = secret

        def get_password(self, name):
            return self.secret

    calls: list[str] = []
    real_load, real_sync = s3sync.load_s3, s3sync.sync
    ini = str(_TMP / "agent.ini")
    try:
        s3sync.load_s3 = lambda path: {"a": conn("a", base)}
        s3sync.sync = lambda c, sec, **kw: (calls.append(c.name)
                                            or s3sync.SyncReport(listed=3, matched=2,
                                                                 downloaded=2))
        run = FakeRun()
        runs._pull_buckets(run, src_with((base, "dir")),
                           cred_store=FakeStore(), ini_path=ini)
        check("run: went to the bucket before reading the folders", calls == ["a"], str(calls))
        check("run: the stage is named separately",
              any(e.get("t") == "stage" and e.get("stage") == "s3" for e in run.events),
              str(run.events))
        check("run: the pass summary is shown to the person",
              any(e.get("t") == "progress" and "downloaded" in (e.get("msg") or "")
                  for e in run.events), str(run.events))

        # Flag cleared -- we do not go to the network at all, but we do not stay silent.
        calls.clear()
        s3sync.load_s3 = lambda path: {"a": conn("a", base, refresh=False)}
        run = FakeRun()
        runs._pull_buckets(run, src_with((base, "dir")),
                           cred_store=FakeStore(), ini_path=ini)
        check("run: with the flag cleared we do not go to the network", calls == [], str(calls))
        check("run: and this is said with a warning, not left unsaid",
              any(e.get("t") == "warning" and e.get("kind") == "s3_not_refreshed"
                  for e in run.events), str(run.events))

        # Bucket unavailable -- a NAMED refusal, not a computation on yesterday's data.
        s3sync.load_s3 = lambda path: {"a": conn("a", base)}

        def boom(c, sec, **kw):
            raise s3sync.S3Error("the storage did not accept the key (403 SignatureDoesNotMatch)")

        s3sync.sync = boom
        run = FakeRun()
        try:
            runs._pull_buckets(run, src_with((base, "dir")),
                               cred_store=FakeStore(), ini_path=ini)
            check("run: an unavailable bucket stops the run", False, "no refusal")
        except runs.RunError as exc:
            check("run: an unavailable bucket stops the run",
                  exc.kind == "s3_unavailable", exc.kind)
            check("run: the refusal names the connection and the reason",
                  "“a”" in exc.message and "403" in (exc.detail or ""),
                  f"{exc.message} / {exc.detail}")
            check("run: the refusal names the way out -- computing on what is already downloaded",
                  "refresh before run" in (exc.detail or ""), exc.detail)

        # The same trouble, but the person EXPLICITLY allowed computing on what is already downloaded.
        run = FakeRun()
        runs._pull_buckets(run, src_with((base, "dir")), stale_ok=True,
                           cred_store=FakeStore(), ini_path=ini)
        check("run: the permission \"on what is already downloaded\" lets the pass through",
              any(e.get("kind") == "s3_stale" for e in run.events), str(run.events))

        # No secret -- also a refusal, not a quiet run on yesterday's data.
        s3sync.sync = lambda c, sec, **kw: s3sync.SyncReport()
        run = FakeRun()
        try:
            runs._pull_buckets(run, src_with((base, "dir")),
                               cred_store=FakeStore(secret=None), ini_path=ini)
            check("run: refusal without a secret", False, "no refusal")
        except runs.RunError as exc:
            check("run: refusal without a secret", exc.kind == "s3_no_secret", exc.kind)
            check("run: the refusal about the secret leads to the screen, not to documentation",
                  "Report folders" in (exc.detail or ""), exc.detail)

        # The report folder is linked to no bucket -- we do not go to the network and
        # stay silent: this is an ordinary run on a local folder.
        calls.clear()
        s3sync.sync = lambda c, sec, **kw: (calls.append(c.name)
                                            or s3sync.SyncReport())
        run = FakeRun()
        runs._pull_buckets(run, src_with((other, "dir")),
                           cred_store=FakeStore(), ini_path=ini)
        check("run: a folder without a bucket -- no network, no extra words",
              calls == [] and run.events == [], str(run.events))
    finally:
        s3sync.load_s3, s3sync.sync = real_load, real_sync


def test_pull_now():
    """The "Download now" button: progress, summary and the time of the last download.

    A separate check, because before it files could be downloaded ONLY by a
    console command. The progress must move (the page shows it), the summary
    must name numbers, and the time of the last download must lie in THE SAME
    place as the state of what was downloaded: "what we have" and "when we
    took it" are one record, and two records of one fact are bound to diverge.
    """
    b = Bucket()
    b.objects["exports_été/α.xlsx"] = b"x" * 1000
    b.objects["exports_été/β.xlsx"] = b"y" * 2000
    srv, base = start_bucket(b)
    root = _TMP / "pull"
    target = root / "dossier"
    creds = root / "creds-root"
    ini = root / "agent.ini"
    ini.parent.mkdir(parents=True, exist_ok=True)
    ini.write_text(
        f"[s3:pull]\nendpoint = {base}\nbucket = bkt\nprefix = exports_été/\n"
        f"access_key = {AK}\ntarget = {target}\n", encoding="utf-8")
    saved = {k: os.environ.get(k) for k in
             ("OP_AGENT_INI", "OP_AGENT_DEV_CRED_STORE")}
    os.environ["OP_AGENT_INI"] = str(ini)
    os.environ["OP_AGENT_DEV_CRED_STORE"] = "1"
    try:
        before = s3ui.sources_state(cred_root=creds)["sources"][0]
        check("before the first download there is no time, and that is stated honestly",
              before.get("last_pull") == {}, str(before.get("last_pull")))
        check("the \"refresh before run\" flag is on by default",
              before.get("refresh_before_run") is True, str(before))

        try:
            s3ui.pull_now({"name": "pull"}, cred_root=creds, state_root=root,
                          background=False)
            check("without a secret we do not go to download", False, "went")
        except s3ui.IniError as e:
            check("without a secret we do not go to download", e.code == "no_s3_secret", e.code)

        s3ui.set_secret("pull", SK, cred_root=creds)
        res = s3ui.pull_now({"name": "pull"}, cred_root=creds, state_root=root,
                            background=False)
        check("the download was started", res.get("started") is True, str(res))
        pull = res.get("pull") or {}
        check("summary: two files downloaded", pull.get("downloaded") == 2, str(pull))
        check("summary: bytes are counted", pull.get("bytes") == 3000, str(pull))
        check("summary: the pass finished, it does not hang", pull.get("running") is False, str(pull))
        check("the files really are in the folder",
              sorted(p.name for p in target.iterdir()) == ["α.xlsx", "β.xlsx"],
              str(sorted(p.name for p in target.iterdir())))

        state = s3ui.pull_state(state_root=root)["pulls"].get("pull") or {}
        check("progress and summary are available through a separate light endpoint",
              state.get("downloaded") == 2, str(state))
        last = state.get("last_pull") or {}
        check("the time of the last download is recorded and ready to show",
              bool(last.get("at")) and last.get("ok") is True, str(last))
        check("and it lies in the same place as the state of the downloaded files",
              bool(s3sync.last_sync("pull", root)), str(s3sync.last_sync("pull", root)))
        blob = json.dumps(state, ensure_ascii=False)
        check("the download progress has neither the secret nor the access key",
              AK not in blob and SK not in blob, blob[:200])

        # A failure records the ATTEMPT: "downloaded today at 9:40" after a refusal
        # would read as success, while the folder holds yesterday's files.
        b.deny = True
        res = s3ui.pull_now({"name": "pull"}, cred_root=creds, state_root=root,
                            background=False)
        pull = res.get("pull") or {}
        check("a storage refusal is a human phrase, not a traceback",
              pull.get("ok") is False and "Traceback" not in (pull.get("error") or "")
              and "AccessDenied" in (pull.get("error") or ""), str(pull))
        last = (s3ui.pull_state(state_root=root)["pulls"].get("pull") or {}).get("last_pull") or {}
        check("a failed attempt is marked as failed, not passed off as a download",
              last.get("ok") is False, str(last))
    finally:
        b.deny = False
        srv.shutdown()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    try:
        for fn in (test_signature, test_signature_against_botocore, test_sync,
                   test_safety_and_errors, test_stop, test_config,
                   test_winscp_mapping, test_connection_check,
                   test_bucket_as_source, test_pull_now):
            print(f"--- {fn.__name__}")
            fn()
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    total = _PASS + _FAIL
    print(f"\n{_PASS}/{total}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
