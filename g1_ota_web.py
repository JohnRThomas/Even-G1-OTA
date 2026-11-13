#!/usr/bin/env python3
"""Browser front end for g1_ota.py.

    g1_ota_web.py [--port 8341] [--no-browser]

Serves http://127.0.0.1:8341/: pick the Bluetooth adapter, scan for G1 arms
and install an update zip or MCUboot .bin on the arms you tick, with the
script's own log streamed to the page.

The BLE work is g1_ota.py's code unchanged, run on a worker thread's event
loop, one job at a time; the page polls /api/state.  It listens on the
loopback interface only, since anyone who can reach it can flash the glasses.

Python 3, standard library only, on top of what g1_ota.py needs (bleak, cbor2).
"""
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import time
import webbrowser
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import g1_ota  # noqa: E402

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Jobs: one at a time, on a worker loop; their log is what the page shows
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, kind, title):
        self.kind, self.title = kind, title
        self.status = "running"          # running | done | error | cancelled
        self.lines = []
        self.progress = ""               # the "\r..." line g1_ota rewrites in place
        self.error = None
        self.future = None
        self.started, self.finished = time.time(), None

    def log(self, msg):
        self.lines.extend(str(msg).split("\n"))

    def print(self, *args, end="\n", **_kw):
        """Stands in for print() inside g1_ota: "\\r..." lines are progress,
        the bare print() that follows them commits the last one."""
        text = " ".join(str(a) for a in args)
        if text.startswith("\r"):
            self.progress = text[1:]
        elif text == "" and end == "\n" and self.progress:
            self.lines.append(self.progress)
            self.progress = ""
        else:
            self.log(text)

    def state(self, since=0):
        return {"kind": self.kind, "title": self.title, "status": self.status,
                "since": since, "total": len(self.lines), "lines": self.lines[since:],
                "progress": self.progress, "error": self.error,
                "seconds": round((self.finished or time.time()) - self.started, 1)}


class Runner:
    """Owns the worker thread, the current job and the last scan."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="g1-ota-ble", daemon=True).start()
        self.lock = threading.Lock()
        self.job = None
        self.arms = []                   # [(name, address)] from the last scan
        self.scan = {"status": "idle", "error": None, "at": None}
        self.adapter = None              # the page's choice; applied to g1_ota.ADAPTER per job
        self.tmp = tempfile.TemporaryDirectory(prefix="g1_ota_web_")

    # -- jobs --------------------------------------------------------------
    def busy(self):
        return self.job is not None and self.job.status == "running"

    def use_adapter(self, adapter):
        """Select the local adapter for the next scan / job (None: system default)."""
        adapter = (adapter or "").strip() or None
        if adapter and adapter not in {n for n, _ in g1_ota.list_adapters()}:
            raise ValueError(f"no such Bluetooth adapter: {adapter}")
        self.adapter = adapter
        g1_ota.ADAPTER = adapter

    def start(self, kind, title, coro_fn):
        with self.lock:
            if self.busy():
                raise RuntimeError("a job is already running")
            job = Job(kind, title)
            g1_ota.log, g1_ota.print = job.log, job.print
            self.job = job
            job.future = asyncio.run_coroutine_threadsafe(self._run(job, coro_fn), self.loop)
        return job

    async def _run(self, job, coro_fn):
        try:
            await coro_fn()
            job.status = "done"
        except asyncio.CancelledError:
            job.status, job.error = "cancelled", "cancelled"
            job.log("\ncancelled")
        except g1_ota.OtaError as e:
            job.status, job.error = "error", str(e)
            job.log(f"\nerror: {e}")
        except Exception as e:  # a bug or a BLE stack failure: show it, keep serving
            job.status, job.error = "error", f"{type(e).__name__}: {e}"
            job.log(f"\nerror: {job.error}")
        finally:
            job.finished = time.time()
            g1_ota.log, g1_ota.print = ORIGINAL_LOG, ORIGINAL_PRINT

    def cancel(self):
        job = self.job
        if job is not None and job.status == "running" and job.future is not None:
            job.future.cancel()
            return True
        return False

    # -- scanning ----------------------------------------------------------
    def start_scan(self):
        if self.scan["status"] == "running":
            return
        if self.busy():
            raise RuntimeError("a job is running; scan when it has finished")
        self.scan = {"status": "running", "error": None, "at": time.time()}
        asyncio.run_coroutine_threadsafe(self._scan(), self.loop)

    async def _scan(self):
        try:
            found = await g1_ota.BleakScanner.discover(timeout=10.0, **g1_ota.adapter_kw())
            self.arms = g1_ota.select_arms(found)
            self.scan = {"status": "done", "error": None, "at": time.time()}
        except Exception as e:
            self.scan = {"status": "error", "error": f"{type(e).__name__}: {e}", "at": time.time()}

    def chosen(self, addresses):
        arms = [a for a in self.arms if a[1] in addresses]
        if not arms:
            raise ValueError("scan and tick at least one arm")
        return arms

    # -- the install job -----------------------------------------------------
    def save_upload(self, upload):
        filename, data = upload
        if not data:
            raise ValueError(f"{filename or 'file'}: empty upload")
        d = tempfile.mkdtemp(dir=self.tmp.name)
        path = os.path.join(d, SAFE_NAME.sub("_", os.path.basename(filename) or "upload") or "upload")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def install(self, upload, addresses):
        arms = self.chosen(addresses)
        path = self.save_upload(upload)

        async def go():
            images = g1_ota.load_images(path)
            for img in images:
                g1_ota.log(f"{img.name}: {g1_ota.CORE[img.index]}, {len(img.data)} bytes, "
                           f"hash {img.hash.hex()[:16]}")
            for name, address in arms:
                await g1_ota.flash_arm(address, name, images)
            g1_ota.log("\ndone")

        return self.start("install", f"install {os.path.basename(path)}", go)

    def state(self, since=0):
        return {"arms": [{"name": n, "address": a} for n, a in self.arms],
                "adapters": [{"name": n, "address": a} for n, a in g1_ota.list_adapters()],
                "adapter": self.adapter,
                "scan": self.scan,
                "job": self.job.state(since) if self.job else None,
                "busy": self.busy()}


ORIGINAL_LOG, ORIGINAL_PRINT = g1_ota.log, print


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def parse_multipart(content_type, body):
    """{field: str} and {field: (filename, bytes)} of a multipart/form-data body."""
    msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + content_type.encode("latin-1")
                                               + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    fields, files = {}, {}
    if not msg.is_multipart():      # a form with no fields at all parses as a bare body
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name is None:
            continue
        data = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[name] = (filename, data)
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    runner = None   # set by make_server
    server_version = "g1-ota-web/1"

    def log_message(self, *args):   # keep the terminal for the job log
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            data = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/state":
            since = 0
            m = re.search(r"(?:^|&)since=(\d+)", query)
            if m:
                since = int(m.group(1))
            self._json(self.runner.state(since))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            ctype = self.headers.get("Content-Type") or ""
            if ctype.startswith("multipart/form-data"):
                fields, files = parse_multipart(ctype, body)
            else:
                fields, files = {}, {}
            if self.path in ("/api/scan", "/api/install") and "adapter" in fields:
                self.runner.use_adapter(fields["adapter"])
            if self.path == "/api/scan":
                self.runner.start_scan()
            elif self.path == "/api/cancel":
                self.runner.cancel()
            elif self.path == "/api/install":
                self.runner.install(files.get("file") or (None, b""), fields.get("arms", "").split(","))
            else:
                self._json({"error": "not found"}, 404)
                return
            self._json(self.runner.state())
        except (ValueError, RuntimeError, g1_ota.OtaError) as e:
            self._json({"error": str(e)}, 400)


def make_server(address=("127.0.0.1", 8341)):
    runner = Runner()
    handler = type("BoundHandler", (Handler,), {"runner": runner})
    server = ThreadingHTTPServer(address, handler)
    server.runner = runner
    return server


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>G1 OTA</title>
<style>
  :root { color-scheme: light dark;
    --bg: #f4f5f7; --card: #ffffff; --ink: #1b1f24; --muted: #5d6673; --line: #d8dde4;
    --accent: #1f6feb; --accent-ink: #ffffff; --ok: #1a7f37; --bad: #c62828; --warn: #9a6700;
    --log-bg: #0f1419; --log-ink: #d6dde5; }
  @media (prefers-color-scheme: dark) { :root {
    --bg: #0f1419; --card: #181d24; --ink: #e6edf3; --muted: #9aa5b1; --line: #2b323b;
    --accent: #4c8dff; --ok: #3fb950; --bad: #ff6b6b; --warn: #e3b341;
    --log-bg: #090c10; --log-ink: #c9d1d9; } }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  header { display: flex; align-items: baseline; gap: 14px; padding: 18px 24px 6px; }
  header h1 { font-size: 20px; margin: 0; font-weight: 650; letter-spacing: .01em; }
  header .sub { color: var(--muted); font-size: 13px; }
  main { display: grid; grid-template-columns: minmax(300px, 420px) 1fr; gap: 18px; padding: 12px 24px 28px; }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
  .col { display: flex; flex-direction: column; gap: 18px; min-width: 0; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 0 0 12px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 8px 0; }
  label.field { display: block; margin: 10px 0 4px; font-size: 13px; color: var(--muted); }
  select { width: 100%; padding: 7px 9px; border: 1px solid var(--line);
    border-radius: 6px; background: var(--card); color: var(--ink); font: inherit; }
  input[type=file] { font: inherit; max-width: 100%; }
  button { padding: 8px 14px; border-radius: 7px; border: 1px solid var(--line); background: var(--card);
    color: var(--ink); font: inherit; cursor: pointer; }
  button.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); font-weight: 600; }
  button.danger { color: var(--bad); border-color: var(--bad); }
  button:disabled { opacity: .5; cursor: default; }
  .arms { list-style: none; padding: 0; margin: 6px 0 0; }
  .arms li { display: flex; align-items: center; gap: 10px; padding: 7px 4px; border-top: 1px solid var(--line); }
  .arms li:first-child { border-top: 0; }
  .arms .addr { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; margin-left: auto; }
  .hint { color: var(--muted); font-size: 13px; margin: 6px 0 0; }
  .warn { color: var(--warn); font-size: 13px; margin: 8px 0 0; }
  .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600;
    border: 1px solid var(--line); color: var(--muted); }
  .pill.running { color: var(--accent); border-color: var(--accent); }
  .pill.done { color: var(--ok); border-color: var(--ok); }
  .pill.error, .pill.cancelled { color: var(--bad); border-color: var(--bad); }
  .log-card { display: flex; flex-direction: column; min-height: 420px; height: calc(100vh - 110px); }
  .log-head { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
  .log-head .title { font-weight: 600; }
  .log-head .time { color: var(--muted); font-size: 13px; }
  .log-head button { margin-left: auto; }
  pre.log { flex: 1; margin: 0; overflow: auto; background: var(--log-bg); color: var(--log-ink);
    border-radius: 8px; padding: 12px 14px; font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    white-space: pre-wrap; word-break: break-word; }
  pre.log .progress { color: var(--accent); }
  pre.log .err { color: var(--bad); }
  .bar { height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; margin-top: 10px; }
  .bar i { display: block; height: 100%; width: 0; background: var(--accent); transition: width .3s; }
  .toast { position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%); background: var(--bad);
    color: #fff; padding: 10px 16px; border-radius: 8px; font-size: 14px; display: none; max-width: 90vw; }
</style>
</head>
<body>
<header>
  <h1>G1 OTA</h1>
  <span class="sub">Even Realities G1 · install updates</span>
</header>
<main>
  <div class="col">
    <section class="card">
      <h2>Arms</h2>
      <label class="field" for="adapter">Bluetooth adapter</label>
      <select id="adapter"><option value="">system default</option></select>
      <div class="row">
        <button id="scan" class="primary">Scan</button>
        <span id="scanmsg" class="hint"></span>
      </div>
      <ul id="arms" class="arms"></ul>
      <p class="hint">An arm accepts a host other than its phone only while it sits in the case with the lid open.</p>
    </section>

    <section class="card">
      <h2>Install update</h2>
      <label class="field" for="upd">application_update.zip, app_update.bin or net_core_app_update.bin</label>
      <input id="upd" type="file" accept=".zip,.bin">
      <div class="row"><button id="install" class="primary">Install on ticked arms</button></div>
      <p class="warn">There is no revert: the bootloader overwrites the running image on reboot.</p>
    </section>
  </div>

  <div class="col">
    <section class="card log-card">
      <h2>Log</h2>
      <div class="log-head">
        <span id="pill" class="pill">idle</span>
        <span id="title" class="title"></span>
        <span id="time" class="time"></span>
        <button id="cancel" class="danger" hidden>Cancel</button>
      </div>
      <pre id="log" class="log"></pre>
      <div class="bar"><i id="bar"></i></div>
    </section>
  </div>
</main>
<div id="toast" class="toast"></div>
<script>
(() => {
  const $ = id => document.getElementById(id);
  let lines = [], total = 0, lastJobStart = null;

  function toast(msg) {
    const t = $('toast'); t.textContent = msg; t.style.display = 'block';
    clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', 6000);
  }
  function ticked() {
    return [...document.querySelectorAll('#arms input:checked')].map(i => i.value);
  }
  function renderAdapters(adapters, current) {
    const sel = $('adapter');
    const html = '<option value="">system default</option>' + adapters.map(a =>
      `<option value="${a.name}">${a.name}${a.address ? ' · ' + a.address : ''}</option>`).join('');
    if (sel.dataset.html !== html) {
      sel.innerHTML = html; sel.dataset.html = html;
      sel.value = current || (adapters.length === 1 ? adapters[0].name : '');
    }
  }
  function renderArms(arms) {
    const ul = $('arms');
    const keep = new Set(ticked());
    const html = arms.map(a => {
      const on = keep.size ? keep.has(a.address) : true;
      return `<li><input type="checkbox" value="${a.address}" ${on ? 'checked' : ''}>` +
             `<span>${a.name}</span><span class="addr">${a.address}</span></li>`;
    }).join('');
    if (ul.dataset.html !== html) { ul.innerHTML = html; ul.dataset.html = html; }
  }
  function renderLog(job) {
    if (!job) return;
    const key = job.title + '|' + job.kind;
    if (lastJobStart !== key) { lines = []; total = 0; lastJobStart = key; }
    if (job.since === total) { lines = lines.concat(job.lines); total = job.total; }
    else { lines = lines.slice(0, job.since).concat(job.lines); total = job.total; }
    const log = $('log');
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
    const esc = s => s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    let html = lines.map(l => /^error:/.test(l) ? `<span class="err">${esc(l)}</span>` : esc(l)).join('\n');
    if (job.progress) html += (html ? '\n' : '') + `<span class="progress">${esc(job.progress)}</span>`;
    log.innerHTML = html;
    if (atBottom) log.scrollTop = log.scrollHeight;
    const m = /\((\d+)%\)/.exec(job.progress || '');
    $('bar').style.width = m ? m[1] + '%' : (job.status === 'done' ? '100%' : '0');
    $('pill').textContent = job.status; $('pill').className = 'pill ' + job.status;
    $('title').textContent = job.title;
    $('time').textContent = job.seconds + ' s';
    $('cancel').hidden = job.status !== 'running';
  }
  async function poll() {
    try {
      const r = await fetch('/api/state?since=' + total, {cache: 'no-store'});
      const s = await r.json();
      renderAdapters(s.adapters, s.adapter);
      renderArms(s.arms);
      $('scan').disabled = s.scan.status === 'running' || s.busy;
      $('scanmsg').textContent = s.scan.status === 'running' ? 'scanning (10 s)…'
        : s.scan.status === 'error' ? 'scan failed: ' + s.scan.error
        : s.scan.status === 'done' && !s.arms.length ? 'no Even G1 found (out of the case, lid open, Even app closed?)'
        : s.scan.status === 'done' ? s.arms.length + ' found' : '';
      $('install').disabled = s.busy;
      renderLog(s.job);
    } catch (e) { $('scanmsg').textContent = 'server unreachable'; }
    setTimeout(poll, 500);
  }
  async function post(path, form) {
    const r = await fetch(path, {method: 'POST', body: form});
    const s = await r.json();
    if (!r.ok) { toast(s.error || 'request failed'); return null; }
    return s;
  }
  function withAdapter(fd) { fd.append('adapter', $('adapter').value); return fd; }
  $('scan').onclick = () => post('/api/scan', withAdapter(new FormData()));
  $('cancel').onclick = () => post('/api/cancel', new FormData());
  $('install').onclick = () => {
    const f = $('upd').files[0];
    if (!f) return toast('choose an update file first');
    if (!ticked().length) return toast('scan and tick at least one arm');
    const fd = withAdapter(new FormData()); fd.append('file', f); fd.append('arms', ticked().join(','));
    post('/api/install', fd);
  };
  poll();
})();
</script>
</body>
</html>
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8341)
    ap.add_argument("--no-browser", action="store_true", help="do not open the page")
    args = ap.parse_args(argv)
    server = make_server(("127.0.0.1", args.port))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"G1 OTA: {url}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
