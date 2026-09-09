"""Shared fixtures: a real local mail server (mox localserve), a fake Gotenberg
conversion service, a fake lp, and a driver that runs poller.py as a
subprocess with --once.

Everything the poller touches is real: IMAP/SMTP protocol against mox,
HTTP conversion calls against the fake Gotenberg, subprocess calls to lp,
HTTP to the health endpoint. Only the far ends are fakes: the mail account
(mox), the converter (fake Gotenberg, real one in the slow suite), and the
printer (fake lp).

Requires a mox binary: `make mox` (pinned, in .tools/mox), or MOX_BIN, or
`mox` on PATH. Python deps: poller/requirements.txt + tests/requirements_test.txt.
"""

import email
import imaplib
import json
import os
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
POLLER = REPO_ROOT / "poller" / "poller.py"
MOX_VERSION = "v0.0.17"
GOTENBERG_VERSION = "8.21.0"  # served by the fake on GET /version

MOX_ACCOUNT = "mox@localhost"
MOX_PASSWORD = "moxmoxmox"
IMAP_PORT = 1993  # implicit TLS (poller IMAP_SSL=true)
SMTP_INJECT_PORT = 1025  # unauthenticated (test-side injection)
SMTP_SUBMIT_PORT = 1587  # STARTTLS + AUTH (poller confirmation replies)


def find_mox():
    env = os.environ.get("MOX_BIN")
    if env and Path(env).is_file():
        return env
    local = REPO_ROOT / ".tools" / "mox"
    if local.is_file():
        return str(local)
    on_path = shutil.which("mox")
    if on_path:
        return on_path
    pytest.fail(
        f"mox binary not found. Run 'make mox' (installs .tools/mox, pinned to "
        f"{MOX_VERSION}) or set MOX_BIN=/path/to/mox."
    )


def _tls_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def uid_hex():
    return uuid.uuid4().hex[:8]


def parse(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


def failed_reply(mox, subj):
    """Fetch the poller's failure reply for `subj` (asserts it exists and has
    the expected subject). Returns the parsed message."""
    raw = mox.find_reply(subj)
    assert raw is not None, "expected a failure reply"
    reply = parse(raw)
    assert reply["Subject"] == f"Print failed: {subj}"
    return reply


class Mox:
    """Handle to the session mox localserve plus test-side mail helpers."""

    def __init__(self, proc, log_path):
        self.proc = proc
        self.log_path = log_path

    def imap(self):
        M = imaplib.IMAP4_SSL("127.0.0.1", IMAP_PORT, ssl_context=_tls_ctx())
        M.login(MOX_ACCOUNT, MOX_PASSWORD)
        return M

    def inject(self, raw_eml):
        """Deliver a raw message to the mox account INBOX via unauthenticated SMTP.

        The envelope recipient is forced to the mox account (one delivery only):
        mox localserve delivers once per unknown recipient, which would duplicate
        a message addressed to both the print alias and the account. Real providers
        do a single delivery + provider-side filtering, which promote() simulates.
        Message headers (To/Cc) are left untouched.
        """
        s = smtplib.SMTP("127.0.0.1", SMTP_INJECT_PORT, timeout=20)
        s.ehlo()
        s.sendmail(MOX_ACCOUNT, [MOX_ACCOUNT], raw_eml)
        s.quit()

    def promote(self, token, folder):
        """Simulate the provider filter: move the INBOX message whose subject contains
        this ASCII token into `folder` (creating it). Returns the message uid.

        mox's IMAP SEARCH has no CHARSET support, so tokens must be ASCII;
        tests keep a non-ASCII display subject but search on its ASCII part.
        """
        M = self.imap()
        try:
            try:
                M.create(folder)
            except imaplib.IMAP4.error:
                pass
            M.select("INBOX")
            typ, data = M.uid("SEARCH", None, "SUBJECT", f'"{token}"')
            uids = data[0].split() if typ == "OK" and data[0] else []
            assert (
                len(uids) == 1
            ), f"expected 1 INBOX message matching {token!r}, got {uids}"
            typ, _ = M.uid("MOVE", uids[0], folder)
            assert typ == "OK"
            return uids[0]
        finally:
            try:
                M.logout()
            except Exception:
                pass

    def append(self, folder, raw_eml):
        """Store raw bytes directly into `folder` via IMAP APPEND, bypassing SMTP.

        Use for headers that the SMTP path would modify or add: mox localserve
        stamps `Authentication-Results: ... spf=pass` on everything received,
        which would defeat REQUIRE_AUTH_PASS tests. Returns the new uid.
        """
        M = self.imap()
        try:
            try:
                M.create(folder)
            except imaplib.IMAP4.error:
                pass
            typ, data = M.append(folder, None, None, raw_eml)
            assert typ == "OK", f"APPEND to {folder} failed: {data}"
            M.select(folder)
            typ, data = M.uid("SEARCH", None, "ALL")
            uids = data[0].split() if typ == "OK" and data[0] else []
            assert uids, f"no messages in {folder} after APPEND"
            return uids[-1]
        finally:
            try:
                M.logout()
            except Exception:
                pass

    def search(self, folder, subject=None):
        M = self.imap()
        try:
            typ, _ = M.select(folder)
            if typ != "OK":
                return []
            if subject is not None:
                typ, data = M.uid("SEARCH", None, "SUBJECT", f'"{subject}"')
            else:
                typ, data = M.uid("SEARCH", None, "ALL")
            uids = data[0].split() if typ == "OK" and data[0] else []
            return [u.decode() if isinstance(u, bytes) else u for u in uids]
        finally:
            try:
                M.logout()
            except Exception:
                pass

    def fetch_raw(self, folder, uid):
        M = self.imap()
        try:
            M.select(folder)
            typ, data = M.uid("FETCH", uid, "(RFC822)")
            assert (
                typ == "OK" and data and data[0]
            ), f"fetch failed in {folder} for {uid}"
            return data[0][1]
        finally:
            try:
                M.logout()
            except Exception:
                pass

    def find_reply(self, subject_fragment):
        """Newest INBOX message whose subject contains the fragment (the poller's
        confirmation replies embed the original subject). Returns raw bytes or None."""
        uids = self.search("INBOX", subject_fragment)
        if not uids:
            return None
        return self.fetch_raw("INBOX", max(uids, key=int))


@pytest.fixture(scope="session")
def mox_log_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("mox")


@pytest.fixture(scope="session")
def mox(mox_log_dir):
    log_path = mox_log_dir / "mox.log"
    logf = open(log_path, "w", buffering=1)
    proc = subprocess.Popen(
        [find_mox(), "localserve", "-dir", str(mox_log_dir / "data")],
        stdout=logf,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 60
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                f"mox exited during startup:\n{log_path.read_text(errors='replace')[-3000:]}"
            )
        try:
            M = imaplib.IMAP4_SSL("127.0.0.1", IMAP_PORT, ssl_context=_tls_ctx())
            try:
                M.login(MOX_ACCOUNT, MOX_PASSWORD)
            finally:
                try:
                    M.logout()
                except Exception:
                    pass
            socket.create_connection(("127.0.0.1", SMTP_INJECT_PORT), timeout=5).close()
            ready = True
            break
        except Exception:
            time.sleep(0.5)
    if not ready:
        proc.kill()
        logf.close()
        pytest.fail(
            f"mox localserve did not become ready within 60s:\n{log_path.read_text(errors='replace')[-3000:]}"
        )
    h = Mox(proc, log_path)
    yield h
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    logf.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    if rep.when == "call" and rep.failed and "mox" in item.fixturenames:
        log = None
        try:
            log = item.funcargs["mox"].log_path.read_text(errors="replace")
        except Exception:
            pass
        if log:
            print("\n--- mox log (tail) ---")
            print("\n".join(log.splitlines()[-40:]))
    return rep


class FakeGotenberg:
    """Fake Gotenberg conversion service (in-process HTTP, stdlib only).

    Serves the two routes the poller uses plus /version:
      GET  /version                       -> GOTENBERG_VERSION as text
      POST /forms/libreoffice/convert     -> 200 + PDF bytes, or scripted failure
      POST /forms/chromium/convert/html   -> 200 + PDF bytes, or scripted failure

    Success responses are `%PDF-1.4 fake-gotenberg <route> of <filename>`
    followed by the uploaded bytes, so tests can tell converted output apart
    and still assert on carried content. `mode` switches failure behavior:
      "ok"     - convert normally (default)
      "http500"- 500 on converts (poller: transient, retryable for lp only;
                  in phase 1 it becomes a per-file error)
      "http400"- 400 on converts (poller: permanent error)
      "nonpdf" - 200 with a non-PDF body (poller: permanent error)
    `convert_hits` records (route, filename) per conversion POST so tests can
    assert what was (and was not) sent for conversion.
    """

    LIBREOFFICE_ROUTE = "/forms/libreoffice/convert"
    CHROMIUM_ROUTE = "/forms/chromium/convert/html"

    def __init__(self):
        self.mode = "ok"
        self.convert_hits = []
        self.version_hits = 0
        handler = self._make_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )

    @property
    def url(self):
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._thread.join(timeout=10)
        self._server.server_close()

    def _make_handler(inner_self):
        fake = inner_self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/pdf"):
                raw = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/version":
                    fake.version_hits += 1
                    self._send(200, GOTENBERG_VERSION, "text/plain")
                else:
                    self._send(404, b"no such route", "text/plain")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                route = self.path
                if route == FakeGotenberg.LIBREOFFICE_ROUTE:
                    nick = "libreoffice"
                elif route == FakeGotenberg.CHROMIUM_ROUTE:
                    nick = "chromium"
                else:
                    self._send(404, b"no such route", "text/plain")
                    return
                filename, content = FakeGotenberg._uploaded(ctype, body)
                fake.convert_hits.append((route, filename))
                if fake.mode == "http500":
                    self._send(500, b"fake gotenberg exploded", "text/plain")
                elif fake.mode == "http400":
                    self._send(400, b"fake gotenberg refuses this file", "text/plain")
                elif fake.mode == "nonpdf":
                    self._send(200, b"this is not a pdf", "text/plain")
                else:
                    marker = (
                        f"%PDF-1.4 fake-gotenberg {nick} of {filename}\n"
                    ).encode()
                    self._send(200, marker + content)

        return Handler

    @staticmethod
    def _uploaded(content_type, body):
        """Extract (filename, bytes) from a multipart/form-data POST body."""
        try:
            msg = email.message_from_bytes(
                b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
            )
            if msg.is_multipart():
                for part in msg.walk():
                    fn = part.get_filename()
                    if fn:
                        return fn, part.get_payload(decode=True) or b""
        except Exception:
            pass
        return "unknown", body


@pytest.fixture()
def gotenberg():
    g = FakeGotenberg().start()
    yield g
    g.stop()


class Fakebin:
    """Fake lp on a PATH prefix + a spool dir to inspect."""

    def __init__(self, tmp_path):
        self.bin = tmp_path / "bin"
        self.spool = tmp_path / "spool"
        self.bin.mkdir()
        dst = self.bin / "lp"
        shutil.copyfile(REPO_ROOT / "tests" / "fakes" / "lp", dst)
        dst.chmod(0o755)

    def drop(self, *names):
        for n in names:
            (self.bin / n).unlink()

    @property
    def env(self):
        return {
            "SPPOOL": str(self.spool),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
        }

    def calls(self):
        f = self.spool / "calls.jsonl"
        if not f.exists():
            return []
        return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]

    def spooled(self):
        if not self.spool.exists():
            return []
        return sorted(f for f in os.listdir(self.spool) if f != "calls.jsonl")

    def spool_bytes(self, name):
        return (self.spool / name).read_bytes()


@pytest.fixture()
def fakebin(tmp_path):
    return Fakebin(tmp_path)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def make_env(fakebin, tmp_path):
    """Full env for a poller run. Unique SOURCE_FOLDER per call isolates tests
    (terminal outcomes expunge, so nothing lingers); the retry state dir is
    per-test too. Pass overrides as keyword args (make_env(DRY_RUN="true")).

    Converting tests must pass GOTENBERG_URL=gotenberg.url (the fake service);
    the default is a closed port, which fails conversions loudly instead of
    silently hitting a real Gotenberg.
    """

    def _make(**over):
        tag = uuid.uuid4().hex[:8]
        env = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "IMAP_HOST": "127.0.0.1",
            "IMAP_PORT": str(IMAP_PORT),
            "IMAP_SSL": "true",
            "TLS_VERIFY": "false",
            "IMAP_USER": MOX_ACCOUNT,
            "IMAP_PASS": MOX_PASSWORD,
            "SMTP_HOST": "127.0.0.1",
            "SMTP_PORT": str(SMTP_SUBMIT_PORT),
            "PRINT_TO": "print@localhost",
            "ALLOWED_SENDERS": "test@localhost",
            "SOURCE_FOLDER": f"t-{tag}-in",
            "PRINTER": "test-queue",
            "CUPS_SERVER": "127.0.0.1:631",
            "MEDIA": "letter",
            "SIDES": "one-sided",
            "GOTENBERG_URL": "http://127.0.0.1:9",
            "CONFIRM_REPLY": "true",
            "PRINT_BODY": "true",
            "DRY_RUN": "false",
            "SKIP_PRINT": "false",
            "REQUIRE_AUTH_PASS": "false",
            "POLL_INTERVAL": "60",
            "HEALTH_BIND": "127.0.0.1",
            "HEALTH_PORT": str(free_port()),
            "MAX_ATTACH_MB": "25",
            "RETRY_LIMIT": "3",
            "LOG_LEVEL": "INFO",
        }
        env.update(fakebin.env)
        env.update({k: str(v) for k, v in over.items()})
        return env

    return _make


@pytest.fixture()
def run_poller():
    def _run(env, args=("--once",), timeout=90):
        return subprocess.run(
            [sys.executable, str(POLLER), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=REPO_ROOT,
        )

    return _run
