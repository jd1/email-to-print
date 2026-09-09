"""Print happy paths: what gets printed, how, and what the sender hears.

Terminal outcomes expunge the message (no processed/rejected folders), so
these tests assert the message is gone from SOURCE_FOLDER plus the reply.
"""

from conftest import FakeGotenberg, parse, uid_hex
from mailgen import MINIMAL_PDF, MINIMAL_PNG, make_docx, make_eml


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


def test_image_attachment_prints(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        attachments=[("photo.jpg", MINIMAL_PNG, "image/jpeg")],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert len(fakebin.calls()) == 1
    assert fakebin.calls()[0][-1].endswith("01-photo.jpg")
    assert fakebin.spool_bytes("000-01-photo.jpg") == MINIMAL_PNG
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0


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


def test_markdown_attachment_renders_then_prints(
    mox, make_env, run_poller, fakebin, gotenberg
):
    """.md goes through Gotenberg's Chromium route (upload is index.html)."""
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        attachments=[("notes.md", b"# shopping\n\n- eggs\n", "text/markdown")],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert gotenberg.convert_hits == [(FakeGotenberg.CHROMIUM_ROUTE, "index.html")]
    assert len(fakebin.calls()) == 1
    assert fakebin.calls()[0][-1].endswith("01-notes.pdf")
    out = fakebin.spool_bytes("000-01-notes.pdf")
    assert out.startswith(b"%PDF-1.4 fake-gotenberg chromium of index.html")
    # the fake echoes the converted page: markdown was rendered to HTML
    assert b"<h1>shopping</h1>" in out
    assert b"eggs" in out
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0


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


def test_text_body_printed(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml("test@localhost", "print@localhost", subj, text="plain text body")
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert gotenberg.convert_hits == [(FakeGotenberg.CHROMIUM_ROUTE, "index.html")]
    assert fakebin.calls()[0][-1].endswith("email-body.pdf")
    out = fakebin.spool_bytes("000-email-body.pdf")
    assert out.startswith(b"%PDF-1.4 fake-gotenberg chromium of index.html")
    assert b"plain text body" in out
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0


def test_multiple_attachments_printed_in_order(
    mox, make_env, run_poller, fakebin, gotenberg
):
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        attachments=[
            ("a.pdf", MINIMAL_PDF, "application/pdf"),
            ("b.png", MINIMAL_PNG, "image/png"),
        ],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert len(fakebin.calls()) == 2
    assert fakebin.calls()[0][-1].endswith("01-a.pdf")
    assert fakebin.calls()[1][-1].endswith("02-b.png")
    reply = parse(mox.find_reply(subj)).get_content()
    assert "Queued for printing: a.pdf, b.png" in reply


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


def test_delivered_to_only_prints(mox, make_env, run_poller, fakebin, gotenberg):
    """Routing that only shows up in provider-stamped headers."""
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "other@localhost",
        subj,
        extra_headers={"Delivered-To": "print@localhost"},
        attachments=[("x.pdf", MINIMAL_PDF, "application/pdf")],
    )
    mox.inject(raw)
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert len(fakebin.calls()) == 1
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0


def test_x_original_to_only_prints(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    raw = make_eml(
        "test@localhost",
        "other@localhost",
        subj,
        extra_headers={"X-Original-To": "print@localhost"},
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


def test_inline_part_with_filename_prints(
    mox, make_env, run_poller, fakebin, gotenberg
):
    """A part with a filename but Content-Disposition: inline still counts as an
    attachment (the poller keys off the filename)."""
    subj = f"print {uid_hex()}"
    env = make_env(GOTENBERG_URL=gotenberg.url)
    # multipart base so attach() is valid (text+html -> multipart/alternative)
    m = make_eml(
        "test@localhost",
        "print@localhost",
        subj,
        text="see below",
        html="<p>see below</p>",
    )
    import email.message

    part = email.message.EmailMessage()
    # set_content(filename=...) creates an attachment Content-Disposition,
    # which we then flip to inline (replace_header needs the header to exist)
    part.set_content(MINIMAL_PNG, maintype="image", subtype="png", filename="pic.png")
    part.replace_header("Content-Disposition", 'inline; filename="pic.png"')
    # re-attach manually: parse the built message, add the part, rebuild
    full = email.message_from_bytes(m, policy=email.policy.default)
    full.attach(part)
    mox.inject(full.as_bytes())
    mox.promote(subj, env["SOURCE_FOLDER"])

    r = run_poller(env)
    assert r.returncode == 0, r.stderr
    assert len(fakebin.calls()) == 1
    assert fakebin.calls()[0][-1].endswith("01-pic.png")


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


def test_skip_print_converts_without_printing(
    mox, make_env, run_poller, fakebin, gotenberg
):
    """SKIP_PRINT: real conversion happens, lp is never invoked."""
    subj = f"print {uid_hex()}"
    env = make_env(SKIP_PRINT="true", GOTENBERG_URL=gotenberg.url)
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
    assert gotenberg.convert_hits == [(FakeGotenberg.LIBREOFFICE_ROUTE, "01-memo.docx")]
    assert fakebin.calls() == []
    assert fakebin.spooled() == []
    assert len(mox.search(env["SOURCE_FOLDER"], subj)) == 0
    reply = parse(mox.find_reply(subj))
    assert reply["Subject"] == f"Print queued: {subj}"
    assert "Skipped printing: memo.docx" in reply.get_content()


def test_media_and_sides_options(mox, make_env, run_poller, fakebin, gotenberg):
    subj = f"print {uid_hex()}"
    env = make_env(MEDIA="a4", SIDES="two-sided-long-edge", GOTENBERG_URL=gotenberg.url)
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
    args = fakebin.calls()[0]
    assert "sides=two-sided-long-edge" in args
    assert "media=a4" in args


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
