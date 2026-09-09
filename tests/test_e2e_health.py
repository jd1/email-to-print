"""Health endpoint + long-running (loop) mode against a background poller.

Status-transition semantics (stale last_poll_ok) are intentionally NOT
asserted here.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from conftest import GOTENBERG_VERSION, POLLER, REPO_ROOT, parse, uid_hex
from mailgen import MINIMAL_PDF, make_eml


def get(port, path, timeout=5):
    """Returns (status_code, body_bytes); 404 comes back as a code, not a raise."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{path}", timeout=timeout
        ) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def wait_for(fn, timeout, what):
    deadline = time.time() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.time() > deadline:
            raise TimeoutError(f"timed out waiting for: {what}")
        time.sleep(0.5)


def test_long_running_poller(mox, make_env, fakebin, gotenberg, tmp_path):
    subj = f"print {uid_hex()}"
    env = make_env(POLL_INTERVAL="1", GOTENBERG_URL=gotenberg.url)
    port = int(env["HEALTH_PORT"])
    logf = open(tmp_path / "poller.log", "w", buffering=1)
    proc = subprocess.Popen(
        [sys.executable, str(POLLER)],
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(REPO_ROOT),
    )
    try:

        def healthy_body():
            try:
                code, body = get(port, "/health")
                return body if code == 200 else None
            except OSError:
                return None

        h0 = json.loads(wait_for(healthy_body, 30, "health endpoint 200"))
        assert h0["printer"] == "test-queue"
        assert h0["source"] == env["SOURCE_FOLDER"]
        assert h0["printed_total"] == 0
        assert h0["pending_messages"] == 0
        assert h0["gotenberg_version"] == GOTENBERG_VERSION

        code, _ = get(port, "/nope")
        assert code == 404

        def process_one(s):
            raw = make_eml(
                "test@localhost",
                "print@localhost",
                s,
                attachments=[("x.pdf", MINIMAL_PDF, "application/pdf")],
            )
            mox.inject(raw)
            mox.promote(s, env["SOURCE_FOLDER"])
            wait_for(
                lambda: not mox.search(env["SOURCE_FOLDER"], s),
                30,
                f"message {s!r} drained by loop",
            )
            reply = parse(mox.find_reply(s))
            assert reply is not None
            assert reply["Subject"] == f"Print queued: {s}"

        # two sequential messages: the loop keeps polling, it isn't --once
        process_one(subj)
        h1 = json.loads(healthy_body())
        assert h1["printed_total"] == 1
        assert h1["pending_messages"] == 0

        process_one(f"print {uid_hex()}")
        h2 = json.loads(healthy_body())
        assert h2["printed_total"] == 2
    except Exception:
        logf.flush()
        tail = (tmp_path / "poller.log").read_text(errors="replace").splitlines()[-20:]
        print("\n--- background poller log (tail) ---")
        print("\n".join(tail))
        raise
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        logf.close()
