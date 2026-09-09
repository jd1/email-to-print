"""Slow suite: the real Gotenberg conversion path. Skipped unless a real
Gotenberg answers at GOTENBERG_URL (default http://127.0.0.1:3000, e.g. via
`docker compose up -d gotenberg`); the fast suite covers the same route
with a fake. `lp` stays fake so nothing can print.
"""

import os
import re
import urllib.request

import pytest

from conftest import parse, uid_hex
from mailgen import make_docx, make_eml

pytestmark = pytest.mark.slow

REAL_GOTENBERG_URL = os.environ.get("GOTENBERG_URL", "http://127.0.0.1:3000")


def _real_gotenberg_reachable():
    """True only if /version answers with a version number. A bare HTTP 200
    is not enough: something else may be listening on the port (this check
    once passed against an unrelated web UI)."""
    try:
        with urllib.request.urlopen(REAL_GOTENBERG_URL + "/version", timeout=5) as r:
            if r.status != 200:
                return False
            return (
                re.match(r"^\d+\.\d+(\.\d+)?", r.read().decode("latin-1").strip())
                is not None
            )
    except OSError:
        return False


def test_real_gotenberg_converts_docx(mox, make_env, run_poller, fakebin):
    if not _real_gotenberg_reachable():
        pytest.skip(f"no real Gotenberg at {REAL_GOTENBERG_URL}")
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=REAL_GOTENBERG_URL)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        attachments=[
            (
                "memo.docx",
                make_docx("hello slow"),
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",
            )
        ],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env, timeout=300)
    assert r.returncode == 0, r.stderr
    assert len(fakebin.calls()) == 1
    out = fakebin.spool_bytes("000-01-memo.pdf")
    assert out.startswith(b"%PDF")
    assert b"fake-gotenberg" not in out  # genuinely converted, not faked
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = parse(mox.find_reply(subj))
    assert reply["Subject"] == f"Print queued: {subj}"
