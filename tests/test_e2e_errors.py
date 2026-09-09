"""Failure paths: conversion errors, lp failures, retry cap, size guard,
unsupported types, empty mail. Terminal outcomes expunge the message;
transient lp failures leave it for the next cycle (up to RETRY_LIMIT).
"""

from conftest import FakeGotenberg, failed_reply, parse, uid_hex
from mailgen import MINIMAL_PDF, make_docx, make_eml


def test_transient_lp_failure_stays_for_retry(
    mox, make_env, run_poller, fakebin, gotenberg
):
    subj = f"print {uid_hex()}"
    env = make_env(FAKE_LP_FAIL="1", GOTENBERG_URL=gotenberg.url)
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
    # the attempt happened (recorded) but nothing printed ...
    assert len(fakebin.calls()) == 1
    assert fakebin.spooled() == []
    # ... and the message stays for the next cycle: no expunge, no reply yet
    assert "attempt=1/3" in r.stderr
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 1
    assert mox.find_reply(subj) is None


def test_retry_cap_expunges_after_limit(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(FAKE_LP_FAIL="1", RETRY_LIMIT="2", GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        attachments=[("x.pdf", MINIMAL_PDF, "application/pdf")],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r1 = run_poller(env)
    assert r1.returncode == 0, r1.stderr
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 1

    r2 = run_poller(env)
    assert r2.returncode == 0, r2.stderr
    assert "RETRY cap" in r2.stderr
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = failed_reply(mox, subj)
    assert "Print failed after 2 attempts" in reply.get_content()
    # the retry-cap reply carries the lp error, not the filename: phase 2
    # never records which jobs were pending when submission failed
    assert "lp failed (rc=1)" in reply.get_content()


def test_missing_lp_binary_retries_then_rejects(
    mox, make_env, run_poller, fakebin, gotenberg
):
    """No `lp` on PATH at all (real FileNotFoundError, not a fake exit code),
    with RETRY_LIMIT=0 meaning reject on the first transient failure."""
    subj = f"print {uid_hex()}"
    env = make_env(RETRY_LIMIT="0", GOTENBERG_URL=gotenberg.url)
    fakebin.drop("lp")
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
    assert fakebin.calls() == []
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = failed_reply(mox, subj)
    assert "Print failed after 1 attempts" in reply.get_content()


def test_gotenberg_500_rejects(mox, make_env, run_poller, fakebin, gotenberg):
    """Server-side conversion failure: phase-1 error, expunged immediately
    (conversion errors are not retried, only lp submission is)."""
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    gotenberg.mode = "http500"
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        text="fallback body",
        attachments=[
            (
                "memo.docx",
                make_docx("hello"),
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",
            )
        ],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert "NO-PRINT" in r.stderr
    assert fakebin.calls() == []
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = failed_reply(mox, subj)
    assert "memo.docx: gotenberg" in reply.get_content()
    assert "HTTP 500" in reply.get_content()


def test_gotenberg_unreachable_rejects(mox, make_env, run_poller, fakebin):
    """Connection-refused conversion endpoint: same shape, different error."""
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL="http://127.0.0.1:9")
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        text="fallback body",
        attachments=[
            (
                "memo.docx",
                make_docx("hello"),
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",
            )
        ],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert fakebin.calls() == []
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = failed_reply(mox, subj)
    assert "memo.docx: gotenberg unreachable" in reply.get_content()


def test_gotenberg_400_rejects(mox, make_env, run_poller, fakebin, gotenberg):
    """Client-side conversion failure (permanent): expunged immediately."""
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    gotenberg.mode = "http400"
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        attachments=[
            (
                "memo.docx",
                make_docx("hello"),
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document",
            )
        ],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert fakebin.calls() == []
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = failed_reply(mox, subj)
    assert "HTTP 400" in reply.get_content()


def test_partial_success_reports_errors(mox, make_env, run_poller, fakebin, gotenberg):
    """One attachment prints, another exceeds the size cap -> partial."""
    subj = f"print {uid_hex()}"
    env = make_env(MAX_ATTACH_MB="1", GOTENBERG_URL=gotenberg.url)
    big = b"\0" * (2 * 1024 * 1024)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        text="body",
        attachments=[
            ("good.pdf", MINIMAL_PDF, "application/pdf"),
            ("big.pdf", big, "application/pdf"),
        ],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert "PARTIAL" in r.stderr
    assert fakebin.spooled() == ["000-01-good.pdf"]
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = parse(mox.find_reply(subj))
    assert reply["Subject"] == f"Print partial: {subj}"
    body = reply.get_content()
    assert "Queued for printing: good.pdf" in body
    assert "big.pdf: exceeds 1.0MB" in body


def test_oversize_attachment_rejected(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(MAX_ATTACH_MB="1", PRINT_BODY="false", GOTENBERG_URL=gotenberg.url)
    big = b"\0" * (2 * 1024 * 1024)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        text="body",
        attachments=[("big.pdf", big, "application/pdf")],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert fakebin.calls() == []  # never even attempted
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = failed_reply(mox, subj)
    assert "big.pdf: exceeds 1.0MB" in reply.get_content()


def test_unsupported_only_rejected_when_no_body(
    mox, make_env, run_poller, fakebin, gotenberg
):
    subj = f"print {uid_hex()}"
    env = make_env(PRINT_BODY="false", GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        text="body",
        attachments=[("data.xyz", b"???", "application/octet-stream")],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert "skip unsupported attachment" in r.stderr
    assert fakebin.calls() == []
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = failed_reply(mox, subj)
    assert "Nothing could be printed" in reply.get_content()


def test_empty_mail_rejected(mox, make_env, run_poller, fakebin, gotenberg):
    """No attachments and no body text/html at all. APPENDed directly: the
    SMTP path normalizes an empty body to CRLF (which the poller would
    render as a blank page), hiding the no-body path under test."""
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml("test@localhost", "print@localhost", subj)
    mox.append(env["SOURCE_FOLDER"], raw)

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert fakebin.calls() == []
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = failed_reply(mox, subj)
    # render_body returns None without raising, so errors stays empty and the
    # reply detail is blank (only PRINT_BODY=false produces the explicit
    # "no printable attachment" message)
    assert "Nothing could be printed." in reply.get_content()
