"""Minimal Chrome DevTools Protocol driver over websockets (stdlib + `websockets`
package only). Launches headless Chrome with --remote-debugging-port, opens one
tab, and exposes navigate/eval/screenshot/console-log collection.
"""
import asyncio
import json
import subprocess
import time
import os
import re
import urllib.request
import websockets

CHROME_BIN = os.environ.get(
    "OP_TEST_CHROME_BIN",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


class ChromeSession:
    def __init__(self, port=9222, user_data_dir=None, download_dir=None):
        self.port = port
        self.user_data_dir = user_data_dir
        self.download_dir = download_dir
        self.proc = None
        self.ws = None
        self._id = 0
        self.console_errors = []
        self._recv_task = None

    def start(self):
        args = [
            CHROME_BIN,
            "--headless=new",
            f"--remote-debugging-port={self.port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--window-size=1280,900",
            "--hide-scrollbars",
            # The UI follows Accept-Language since the i18n sprint; the smokes
            # assert the English source strings, so pin the browser locale
            # (override with OP_CHROME_LANG=ru etc. to exercise a catalogue).
            f"--lang={os.environ.get('OP_CHROME_LANG', 'en')}",
            f"--accept-lang={os.environ.get('OP_CHROME_LANG', 'en')}",
        ]
        if self.user_data_dir:
            args.append(f"--user-data-dir={self.user_data_dir}")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        last_err = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=1) as r:
                    json.loads(r.read())
                    return
            except Exception as e:
                last_err = e
                time.sleep(0.2)
        raise RuntimeError(f"chrome didn't come up: {last_err}")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()

    async def _new_tab_ws(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/json/new?about:blank",
                                      method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            info = json.loads(r.read())
        return info["webSocketDebuggerUrl"], info["id"]

    async def connect(self):
        ws_url, self.tab_id = await self._new_tab_ws()
        self.ws = await websockets.connect(ws_url, max_size=None)
        self._pending = {}
        self._recv_task = asyncio.create_task(self._recv_loop())
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("Log.enable")
        await self.send("Network.enable")
        if self.download_dir:
            await self.send("Page.setDownloadBehavior",
                             {"behavior": "allow", "downloadPath": self.download_dir})

    async def _recv_loop(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                else:
                    method = msg.get("method")
                    if method == "Runtime.consoleAPICalled":
                        p = msg["params"]
                        if p.get("type") == "error":
                            parts = [a.get("value") or a.get("description") or "" for a in p.get("args", [])]
                            self.console_errors.append(" ".join(str(x) for x in parts))
                    elif method == "Runtime.exceptionThrown":
                        ed = msg["params"].get("exceptionDetails", {})
                        text = ed.get("exception", {}).get("description") or ed.get("text") or "exception"
                        self.console_errors.append(text)
                    elif method == "Log.entryAdded":
                        e = msg["params"].get("entry", {})
                        if e.get("level") == "error":
                            # Network entries say only "Failed to load
                            # resource: … 404" — WHICH resource lives in the
                            # entry's own `url`, and an error nothing can
                            # attribute is an error nothing can triage.
                            text = e.get("text", "")
                            url = e.get("url") or ""
                            # The page probes the optional desktop agent on
                            # 127.0.0.1:47113-47115; with no agent running
                            # (always, in CI) the browser logs a refused
                            # connection. That is expected, not a page bug.
                            if (re.search(r"127\.0\.0\.1:4711[3-5]/health", url + " " + text)
                                    and "ERR_CONNECTION_REFUSED" in text + url):
                                continue
                            self.console_errors.append(f"{text} {url}".strip() if url else text)
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send(self, method, params=None, timeout=30):
        self._id += 1
        mid = self._id
        fut = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        msg = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in msg:
            raise RuntimeError(f"{method} failed: {msg['error']}")
        return msg.get("result", {})

    async def navigate(self, url, wait_ms=800):
        await self.send("Page.navigate", {"url": url})
        await asyncio.sleep(wait_ms / 1000)

    async def eval(self, expr, await_promise=False):
        r = await self.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": await_promise,
        })
        if r.get("exceptionDetails"):
            raise RuntimeError(f"eval failed: {r['exceptionDetails']}")
        return r.get("result", {}).get("value")

    async def click(self, selector):
        return await self.eval(
            f"(function(){{const e=document.querySelector({json.dumps(selector)});"
            "if(!e) return 'NOT_FOUND'; e.scrollIntoView({block:'center'}); e.click(); return 'OK';})()"
        )

    async def screenshot(self, path, full_page=False):
        params = {"format": "png"}
        if full_page:
            metrics = await self.send("Page.getLayoutMetrics")
            cs = metrics.get("cssContentSize") or metrics.get("contentSize")
            params["clip"] = {"x": 0, "y": 0, "width": cs["width"], "height": cs["height"], "scale": 1}
        r = await self.send("Page.captureScreenshot", params)
        import base64
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))

    async def close(self):
        if self._recv_task:
            self._recv_task.cancel()
        if self.ws:
            await self.ws.close()
