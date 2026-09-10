"""Reject paths: allow-list and SPF/DKIM auth checks. Fail closed.

The drained-INBOX model expunges everything terminal: rejects vanish from
SOURCE_FOLDER (no rejected folder) and strangers/auth-failures never get a
reply.
"""

from conftest import uid_hex
from mailgen import MINIMAL_PDF, make_eml


def test_unknown_sender_dropped_silently(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "intruder@localhost",
        "print@localhost",
        subj,
        attachments=[("x.pdf", MINIMAL_PDF, "application/pdf")],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert "DROP sender not allowed" in r.stderr
    assert fakebin.calls() == []
    # expunged, not moved anywhere; no reply to an unverified address
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    assert mox.find_reply(subj) is None


def test_empty_allowlist_fail_closed(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(ALLOWED_SENDERS="", GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        attachments=[("x.pdf", MINIMAL_PDF, "application/pdf")],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert "fail-closed" in r.stderr
    assert fakebin.calls() == []
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    assert mox.find_reply(subj) is None


def test_auth_pass_spf_prints(mox, make_env, run_poller, fakebin, gotenberg):
    """APPENDed directly (no SMTP) so mox cannot re-stamp the AR header."""
    subj = f"print {uid_hex()}"
    env = make_env(REQUIRE_AUTH_PASS="true", GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        extra_headers={
            "Authentication-Results": "localhost; spf=pass smtp.mailfrom=test@localhost"
        },
        attachments=[("x.pdf", MINIMAL_PDF, "application/pdf")],
    )
    mox.append(env["SOURCE_FOLDER"], raw)

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert len(fakebin.calls()) == 1
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
