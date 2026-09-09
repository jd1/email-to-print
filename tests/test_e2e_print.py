"""Print happy paths: what gets printed, how, and what the sender hears.

Terminal outcomes expunge the message (no processed/rejected folders), so
these tests assert the message is gone from SOURCE_FOLDER plus the reply.
"""

from conftest import FakeGotenberg, parse, uid_hex
from mailgen import MINIMAL_PDF, make_docx, make_eml


def test_pdf_attachment_prints(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        text="body",
        attachments=[("homework.pdf", MINIMAL_PDF, "application/pdf")],
    )
    mid = parse(raw)["Message-ID"]
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr

    calls = fakebin.calls()
    assert len(calls) == 1
    args = calls[0]
    assert args[:3] == ["-d", "test-queue", "-o"]
    assert "sides=one-sided" in args and "media=letter" in args
    assert args[-2] == "--"
    assert args[-1].endswith("01-homework.pdf")
    spooled = fakebin.spooled()
    assert spooled == ["000-01-homework.pdf"]
    assert fakebin.spool_bytes("000-01-homework.pdf") == MINIMAL_PDF
    # native PDF needs no conversion
    assert gotenberg.convert_hits == []

    # terminal outcome expunges the message: nothing left in source
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0

    # confirmation reply to the sender
    reply = parse(mox.find_reply(subj))
    assert reply is not None
    assert "test@localhost" in reply["To"]
    assert reply["Subject"] == f"Print queued: {subj}"
    assert reply["In-Reply-To"] == mid
    assert "Queued for printing: homework.pdf" in reply.get_content()


def test_office_doc_converts_then_prints(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    docx = make_docx("permission slip")
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        attachments=[
            (
                "permission.docx",
                docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        ],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    # conversion went through Gotenberg's LibreOffice route ...
    assert gotenberg.convert_hits == [
        (FakeGotenberg.LIBREOFFICE_ROUTE, "01-permission.docx")
    ]
    # ... and the converted PDF is what got printed
    assert len(fakebin.calls()) == 1
    assert fakebin.calls()[0][-1].endswith("01-permission.pdf")
    out = fakebin.spool_bytes("000-01-permission.pdf")
    assert out.startswith(b"%PDF-1.4 fake-gotenberg libreoffice of 01-permission.docx")
    assert out.endswith(docx)
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    assert (
        "Queued for printing: permission.docx"
        in parse(mox.find_reply(subj)).get_content()
    )


def test_html_body_printed(mox, make_env, run_poller, fakebin, gotenberg):
    """No attachment -> the email body itself is rendered and printed."""
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    html = "<html><body><h1>Meeting notes</h1><p>bring snacks</p></body></html>"
    raw = make_eml("test@localhost", "print@localhost", subj, html=html)
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert gotenberg.convert_hits == [(FakeGotenberg.CHROMIUM_ROUTE, "index.html")]
    assert len(fakebin.calls()) == 1
    # the body is printed from workdir/email-body.pdf (no attachment index prefix)
    assert fakebin.calls()[0][-1].endswith("email-body.pdf")
    out = fakebin.spool_bytes("000-email-body.pdf")
    assert out.startswith(b"%PDF-1.4 fake-gotenberg chromium of index.html")
    assert b"bring snacks" in out
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0


def test_cc_addressed_prints(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "other@localhost",
        subj,
        cc="print@localhost",
        attachments=[("x.pdf", MINIMAL_PDF, "application/pdf")],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert len(fakebin.calls()) == 1
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0


def test_encoded_subject_and_sender(mox, make_env, run_poller, fakebin, gotenberg):
    tok = uid_hex()
    subj = f"Überprüfung {tok}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "Tësß Tester <test@localhost>",
        "print@localhost",
        subj,
        attachments=[("x.pdf", MINIMAL_PDF, "application/pdf")],
    )
    mox.inject(raw)
    mox.promote(tok, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert len(fakebin.calls()) == 1
    # non-ASCII subject survives the round trip decoded in the reply
    reply = parse(mox.find_reply(tok))
    assert reply["Subject"] == f"Print queued: {subj}"


def test_path_traversal_filename_neutralized(
    mox, make_env, run_poller, fakebin, gotenberg
):
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        attachments=[("../../evil.pdf", MINIMAL_PDF, "application/pdf")],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert len(fakebin.calls()) == 1
    # basename + sanitize: no directory components survive
    assert fakebin.calls()[0][-1].endswith("01-evil.pdf")
    assert fakebin.spooled() == ["000-01-evil.pdf"]
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0


def test_starttls_connection_prints(mox, make_env, run_poller, fakebin, gotenberg):
    """The IMAP_SSL=false branch: plaintext + STARTTLS on 1143."""
    subj = f"print {uid_hex()}"
    env = make_env(IMAP_SSL="false", IMAP_PORT="1143", GOTENBERG_URL=gotenberg.url)
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
    assert len(fakebin.calls()) == 1
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    assert parse(mox.find_reply(subj))["Subject"] == f"Print queued: {subj}"


def test_dry_run_prints_nothing(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(DRY_RUN="true", GOTENBERG_URL=gotenberg.url)
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
    # DRY_RUN: no conversion POSTs (only the startup /version check) and
    # nothing handed to lp, but the message is still processed
    assert gotenberg.convert_hits == []
    assert gotenberg.version_hits == 1
    assert fakebin.calls() == []
    assert fakebin.spooled() == []
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = parse(mox.find_reply(subj))
    assert reply["Subject"] == f"Print queued: {subj}"


def test_dedup_second_run_prints_nothing(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
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
    assert len(fakebin.calls()) == 1

    r2 = run_poller(env)
    assert r2.returncode == 0, r2.stderr
    assert len(fakebin.calls()) == 1
    assert len(mox.search(env["SOURCE_FOLDER"])) == 0
