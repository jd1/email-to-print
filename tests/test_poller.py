#!/usr/bin/env python3
"""Unit tests for the Gotenberg conversion path. No IMAP/SMTP/network:
`requests` is stubbed before poller is imported, `lp` is mocked per-test."""

import email
import os
import sys
import tempfile
import time
import types
import unittest
from email.message import EmailMessage
from unittest import mock

os.environ.update(
    {
        "IMAP_USER": "u@example.com",
        "IMAP_PASS": "x",
        "PRINT_TO": "print@example.com",
        "PRINTER": "TEST",
        "ALLOWED_SENDERS": "a@example.com",
        "CONFIRM_REPLY": "false",
        "DRY_RUN": "false",
        "GOTENBERG_URL": "http://gotenberg.test:3000",
        "GOTENBERG_TIMEOUT": "5",
    }
)


class _RequestException(Exception):
    pass


_exceptions_module = types.ModuleType("requests.exceptions")
_exceptions_module.RequestException = _RequestException
_fake_requests_module = types.ModuleType("requests")
_fake_requests_module.exceptions = _exceptions_module

http_calls = []
last_upload = {"filename": None, "content": None}
stub = {"post_handler": None, "get_handler": None}


def _fake_post(url, files=None, timeout=None):
    upload_name, upload_content = (files or {}).get("files", (None, None))
    http_calls.append((url, upload_name))
    last_upload["filename"] = upload_name
    last_upload["content"] = upload_content
    return stub["post_handler"](url, files)


def _fake_get(url, timeout=None):
    http_calls.append((url, None))
    return stub["get_handler"](url)


_fake_requests_module.post = _fake_post
_fake_requests_module.get = _fake_get
sys.modules.setdefault("requests", _fake_requests_module)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "poller"))
import poller  # noqa: E402 - sys.path setup must precede local import

FAKE_PDF = b"%PDF-1.4 fake"
LIBREOFFICE_URL = "http://gotenberg.test:3000/forms/libreoffice/convert"
CHROMIUM_URL = "http://gotenberg.test:3000/forms/chromium/convert/html"


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.content = body
        self.text = body[:200].decode("latin-1")


def _response(status_code, body):
    return FakeResponse(status_code, body)


class ConversionTest(unittest.TestCase):
    def setUp(self):
        self.output_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.output_dir.cleanup)
        self.ok_handler = lambda url, files: _response(200, FAKE_PDF)

    def _write_source(self, filename, data=b"attachment-bytes"):
        source_path = os.path.join(self.output_dir.name, filename)
        with open(source_path, "wb") as source_file:
            source_file.write(data)
        return source_path

    def test_office_success(self):
        stub["post_handler"], http_calls[:] = self.ok_handler, []
        output_path = poller.to_pdf(self._write_source("report.docx"), self.output_dir.name)
        self.assertTrue(output_path and output_path.endswith("report.pdf"))
        with open(output_path, "rb") as output_file:
            self.assertEqual(output_file.read(), FAKE_PDF)
        self.assertEqual(http_calls, [(LIBREOFFICE_URL, "report.docx")])

    def test_office_connection_error(self):
        def raise_connection_error(url, files):
            raise _RequestException("gotenberg is down")

        stub["post_handler"] = raise_connection_error
        with self.assertRaises(poller.TransientError):
            poller.to_pdf(self._write_source("a.docx"), self.output_dir.name)

    def test_office_http_500(self):
        stub["post_handler"] = lambda url, files: _response(500, b"boom")
        with self.assertRaises(poller.TransientError):
            poller.to_pdf(self._write_source("a.docx"), self.output_dir.name)

    def test_office_http_422(self):
        stub["post_handler"] = lambda url, files: _response(422, b"invalid document")
        with self.assertRaises(poller.PermanentError):
            poller.to_pdf(self._write_source("a.docx"), self.output_dir.name)

    def test_office_http_400(self):
        stub["post_handler"] = lambda url, files: _response(400, b"bad request")
        with self.assertRaises(poller.PermanentError):
            poller.to_pdf(self._write_source("a.docx"), self.output_dir.name)

    def test_office_non_pdf_response_body(self):
        stub["post_handler"] = lambda url, files: _response(200, b"not a pdf")
        with self.assertRaises(poller.PermanentError):
            poller.to_pdf(self._write_source("a.docx"), self.output_dir.name)

    def test_dry_run_skips_http(self):
        poller.DRY_RUN = True
        self.addCleanup(lambda: setattr(poller, "DRY_RUN", False))

        def unexpected_call(url, files):
            raise AssertionError("no http calls expected in dry-run mode")

        stub["post_handler"] = unexpected_call
        http_calls[:] = []
        output_path = poller.to_pdf(self._write_source("a.docx"), self.output_dir.name)
        self.assertTrue(output_path)
        self.assertEqual(http_calls, [])


class BodyTest(unittest.TestCase):
    def setUp(self):
        self.output_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.output_dir.cleanup)
        self.ok_handler = lambda url, files: _response(200, FAKE_PDF)

    def test_html_body(self):
        stub["post_handler"], http_calls[:] = self.ok_handler, []
        msg = email.message_from_string(
            "From: a@example.com\r\nTo: print@example.com\r\n"
            "Content-Type: multipart/alternative; boundary=b\r\n\r\n"
            "--b\r\nContent-Type: text/html\r\n\r\n<h1>Hi &amp; bye</h1>\r\n--b--\r\n"
        )
        output_path = poller.render_body(msg, self.output_dir.name)
        self.assertTrue(output_path and output_path.endswith("email-body.pdf"))
        self.assertEqual(http_calls, [(CHROMIUM_URL, "index.html")])
        self.assertIn(b"<h1>Hi", last_upload["content"])

    def test_text_body_escaped(self):
        stub["post_handler"], http_calls[:] = self.ok_handler, []
        msg = email.message_from_string(
            "From: a@example.com\r\nTo: print@example.com\r\n"
            "Content-Type: text/plain\r\n\r\nHello <b>& friends\r\n"
        )
        output_path = poller.render_body(msg, self.output_dir.name)
        self.assertTrue(output_path)
        self.assertEqual(http_calls, [(CHROMIUM_URL, "index.html")])
        self.assertIn(b"<pre>Hello &lt;b&gt;&amp; friends", last_upload["content"])

    def test_no_body(self):
        def unexpected_call(url, files):
            raise AssertionError("no body, so no http expected")

        stub["post_handler"] = unexpected_call
        self.assertIsNone(
            poller.render_body(email.message_from_string("Subject: x\r\n"), self.output_dir.name)
        )

    def test_empty_mail_reply_names_the_problem(self):
        # render_body returns None without raising: process() must still say
        # why nothing printed instead of sending a blank error detail.
        poller.CONFIRM_REPLY = True
        self.addCleanup(lambda: setattr(poller, "CONFIRM_REPLY", False))
        msg = EmailMessage()
        msg["From"] = "a@example.com"
        msg["To"] = "print@example.com"
        msg["Subject"] = "empty"
        msg["Message-ID"] = "<empty-body@example.com>"
        fake_imap = FakeImap(msg.as_bytes())
        with mock.patch.object(poller.smtplib, "SMTP", FakeSmtp):
            poller.process(fake_imap, "1")
        self.assertEqual(fake_imap.expunged, ["1"])
        reply = FakeSmtp.instances[-1].sent_messages[-1]
        self.assertEqual(reply["Subject"], "Print failed: empty")
        self.assertIn(
            "no printable attachment and no renderable body", reply.get_content()
        )


class FakeImap:
    def __init__(self, raw_bytes, capabilities=(b"IMAP4rev1", b"UIDPLUS")):
        self.raw_bytes = raw_bytes
        self.capabilities = list(capabilities)
        self.selected = None
        self.fetched_headers = []
        self.fetched_full = []
        self.deleted = set()
        self.expunged = []

    def _key(self, uid):
        return uid.decode() if isinstance(uid, bytes) else uid

    def _header_bytes(self):
        for sep in (b"\r\n\r\n", b"\n\n"):
            if sep in self.raw_bytes:
                return self.raw_bytes.split(sep, 1)[0] + sep
        return self.raw_bytes

    def select(self, folder):
        self.selected = folder
        return "OK", []

    def capability(self):
        return "OK", [b" ".join(self.capabilities)]

    def uid(self, command, *args):
        if command == "FETCH":
            if "HEADER" in args[1]:
                self.fetched_headers.append(self._key(args[0]))
                return "OK", [(b"1 (RFC822.HEADER)", self._header_bytes())]
            self.fetched_full.append(self._key(args[0]))
            return "OK", [(b"1 (RFC822)", self.raw_bytes)]
        if command == "STORE":
            self.deleted.add(self._key(args[0]))
            return "OK", []
        if command == "EXPUNGE":
            uid = self._key(args[0]) if args else None
            if uid in self.deleted:
                self.deleted.discard(uid)
                self.expunged.append(uid)
            return "OK", []
        if command == "SEARCH":
            remaining = [] if "1" in self.expunged else [b"1"]
            return "OK", [b" ".join(remaining)]
        raise AssertionError(f"unexpected IMAP command: {command}")


class FakeSmtp:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.sent_messages = []
        FakeSmtp.instances.append(self)

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        self.sent_messages.append(message)

    def quit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.quit()
        return False


class RoutingTest(unittest.TestCase):
    def _build_message(self, attachments):
        msg = EmailMessage()
        msg["From"] = "a@example.com"
        msg["To"] = "print@example.com"
        msg["Subject"] = "t"
        msg.set_content("a body")
        for filename, content_type, data in attachments:
            maintype, subtype = content_type.split("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        return msg

    def test_docx_and_txt_converted(self):
        stub["post_handler"] = lambda url, files: _response(200, FAKE_PDF)
        http_calls[:] = []
        msg = self._build_message(
            [
                (
                    "rep.docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    b"x",
                ),
                ("notes.txt", "text/plain", b"hi"),
            ]
        )

        fake_imap = FakeImap(msg.as_bytes())
        with mock.patch.object(poller, "print_file", return_value="ok") as print_mock:
            poller.process(fake_imap, "1")
        self.assertEqual(
            http_calls,
            [(LIBREOFFICE_URL, "01-rep.docx"), (CHROMIUM_URL, "index.html")],
        )
        self.assertEqual(print_mock.call_count, 2)
        self.assertEqual(fake_imap.expunged, ["1"])

    def test_skip_print_converts_and_skips_lp(self):
        poller.SKIP_PRINT = True
        self.addCleanup(lambda: setattr(poller, "SKIP_PRINT", False))
        poller.CONFIRM_REPLY = True
        self.addCleanup(lambda: setattr(poller, "CONFIRM_REPLY", False))
        stub["post_handler"] = lambda url, files: _response(200, FAKE_PDF)
        http_calls[:] = []
        msg = self._build_message(
            [
                (
                    "rep.docx",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    b"x",
                ),
            ]
        )
        fake_imap = FakeImap(msg.as_bytes())
        with mock.patch.object(poller.smtplib, "SMTP", FakeSmtp):
            poller.process(fake_imap, "1")
        # Real conversion happened; the print step short-circuits before `lp`.
        self.assertEqual(http_calls, [(LIBREOFFICE_URL, "01-rep.docx")])
        self.assertEqual(fake_imap.expunged, ["1"])
        sent_messages = FakeSmtp.instances[-1].sent_messages
        self.assertEqual(len(sent_messages), 1)
        self.assertTrue(sent_messages[0].get_content().startswith("Skipped printing: rep.docx"))

    def _no_http(self, url, files):
        raise AssertionError("no conversion expected on the native path")

    def test_image_attachment_prints_natively(self):
        stub["post_handler"] = self._no_http
        http_calls[:] = []
        msg = self._build_message([("photo.jpg", "image/jpeg", b"\xff\xd8fake")])
        fake_imap = FakeImap(msg.as_bytes())
        with mock.patch.object(poller, "print_file", return_value="ok") as print_mock:
            poller.process(fake_imap, "1")
        self.assertEqual(http_calls, [])
        self.assertEqual(print_mock.call_count, 1)
        self.assertTrue(print_mock.call_args[0][0].endswith("01-photo.jpg"))
        self.assertEqual(fake_imap.expunged, ["1"])

    def _routed_message(self, **headers):
        msg = EmailMessage()
        msg["From"] = "a@example.com"
        msg["To"] = headers.pop("To", "someone@else.example")
        msg["Subject"] = "t"
        for key, value in headers.items():
            msg[key.replace("_", "-")] = value
        return msg

    def test_cc_address_routes(self):
        msg = self._routed_message(Cc="print@example.com")
        msg.set_content("body")
        fake_imap = FakeImap(msg.as_bytes())
        stub["post_handler"] = lambda url, files: _response(200, FAKE_PDF)
        with mock.patch.object(poller, "print_file", return_value="ok") as print_mock:
            poller.process(fake_imap, "1")
        self.assertEqual(print_mock.call_count, 1)
        self.assertEqual(fake_imap.expunged, ["1"])

    def test_delivered_to_header_routes(self):
        msg = self._routed_message(**{"Delivered_To": "print@example.com"})
        msg.set_content("body")
        fake_imap = FakeImap(msg.as_bytes())
        stub["post_handler"] = lambda url, files: _response(200, FAKE_PDF)
        with mock.patch.object(poller, "print_file", return_value="ok") as print_mock:
            poller.process(fake_imap, "1")
        self.assertEqual(print_mock.call_count, 1)
        self.assertEqual(fake_imap.expunged, ["1"])

    def test_x_original_to_header_routes(self):
        msg = self._routed_message(**{"X_Original_To": "print@example.com"})
        msg.set_content("body")
        fake_imap = FakeImap(msg.as_bytes())
        stub["post_handler"] = lambda url, files: _response(200, FAKE_PDF)
        with mock.patch.object(poller, "print_file", return_value="ok") as print_mock:
            poller.process(fake_imap, "1")
        self.assertEqual(print_mock.call_count, 1)
        self.assertEqual(fake_imap.expunged, ["1"])

    def test_inline_part_with_filename_counts_as_attachment(self):
        stub["post_handler"] = self._no_http
        msg = EmailMessage()
        msg["From"] = "a@example.com"
        msg["To"] = "print@example.com"
        msg["Subject"] = "t"
        msg.set_content("see below")
        msg.add_attachment(
            b"fakepng",
            maintype="image",
            subtype="png",
            filename="pic.png",
            disposition="inline",
        )
        fake_imap = FakeImap(msg.as_bytes())
        with mock.patch.object(poller, "print_file", return_value="ok") as print_mock:
            poller.process(fake_imap, "1")
        self.assertEqual(print_mock.call_count, 1)
        self.assertTrue(print_mock.call_args[0][0].endswith("01-pic.png"))
        self.assertEqual(fake_imap.expunged, ["1"])

    def test_multiple_attachments_print_in_order(self):
        stub["post_handler"] = self._no_http
        msg = self._build_message(
            [
                ("a.pdf", "application/pdf", b"%PDF-1.4 a"),
                ("b.png", "image/png", b"\x89pngb"),
            ]
        )
        fake_imap = FakeImap(msg.as_bytes())
        with mock.patch.object(poller, "print_file", return_value="ok") as print_mock:
            poller.process(fake_imap, "1")
        printed = [c[0][0] for c in print_mock.call_args_list]
        self.assertEqual(len(printed), 2)
        self.assertTrue(printed[0].endswith("01-a.pdf"))
        self.assertTrue(printed[1].endswith("02-b.png"))
        self.assertEqual(fake_imap.expunged, ["1"])

    def test_unsupported_attachment_falls_back_to_body(self):
        stub["post_handler"] = lambda url, files: _response(200, FAKE_PDF)
        http_calls[:] = []
        msg = self._build_message([("data.xyz", "application/octet-stream", b"???")])
        fake_imap = FakeImap(msg.as_bytes())
        with mock.patch.object(poller, "print_file", return_value="ok") as print_mock:
            poller.process(fake_imap, "1")
        # unsupported file skipped, text body rendered via Chromium instead
        self.assertEqual(http_calls, [(CHROMIUM_URL, "index.html")])
        self.assertEqual(print_mock.call_count, 1)
        self.assertTrue(print_mock.call_args[0][0].endswith("email-body.pdf"))
        self.assertEqual(fake_imap.expunged, ["1"])

    def test_oversize_attachment_rejected(self):
        poller.PRINT_BODY = False
        self.addCleanup(lambda: setattr(poller, "PRINT_BODY", True))
        poller.MAX_MB = 1.0
        self.addCleanup(lambda: setattr(poller, "MAX_MB", 25))
        poller.CONFIRM_REPLY = True
        self.addCleanup(lambda: setattr(poller, "CONFIRM_REPLY", False))
        msg = self._build_message(
            [("big.pdf", "application/pdf", b"\0" * (2 * 1024 * 1024))]
        )
        fake_imap = FakeImap(msg.as_bytes())
        with mock.patch.object(poller.smtplib, "SMTP", FakeSmtp):
            poller.process(fake_imap, "1")
        self.assertEqual(fake_imap.expunged, ["1"])
        reply = FakeSmtp.instances[-1].sent_messages[-1]
        self.assertEqual(reply["Subject"], "Print failed: t")
        self.assertIn("big.pdf: exceeds 1.0MB", reply.get_content())

    def test_hostile_filenames_neutralized(self):
        stub["post_handler"] = self._no_http
        cases = [
            ("../../evil.pdf", "01-evil.pdf"),
            ("/abs/path.pdf", "01-path.pdf"),
            ("...", "01-attachment.bin"),
            ("a" * 200 + ".pdf", "01-" + "a" * 116 + ".pdf"),
        ]
        for hostile, expected in cases:
            with self.subTest(filename=hostile):
                msg = self._build_message([(hostile, "application/pdf", b"%PDF-1.4 x")])
                fake_imap = FakeImap(msg.as_bytes())
                with mock.patch.object(
                    poller, "print_file", return_value="ok"
                ) as print_mock:
                    poller.process(fake_imap, "1")
                self.assertEqual(print_mock.call_count, 1)
                self.assertTrue(
                    print_mock.call_args[0][0].endswith(expected),
                    print_mock.call_args[0][0],
                )
                self.assertEqual(fake_imap.expunged, ["1"])

    def test_partial_reply_joins_printed_and_errors(self):
        poller.MAX_MB = 1.0
        self.addCleanup(lambda: setattr(poller, "MAX_MB", 25))
        poller.CONFIRM_REPLY = True
        self.addCleanup(lambda: setattr(poller, "CONFIRM_REPLY", False))
        msg = self._build_message(
            [
                ("good.pdf", "application/pdf", b"%PDF-1.4 good"),
                ("big.pdf", "application/pdf", b"\0" * (2 * 1024 * 1024)),
            ]
        )
        fake_imap = FakeImap(msg.as_bytes())
        with mock.patch.object(
            poller, "print_file", return_value="ok"
        ), mock.patch.object(poller.smtplib, "SMTP", FakeSmtp):
            poller.process(fake_imap, "1")
        self.assertEqual(fake_imap.expunged, ["1"])
        reply = FakeSmtp.instances[-1].sent_messages[-1]
        self.assertEqual(reply["Subject"], "Print partial: t")
        body = reply.get_content()
        self.assertIn("Queued for printing: good.pdf", body)
        self.assertIn("big.pdf: exceeds 1.0MB", body)


class ExtensionTest(unittest.TestCase):
    def test_office_extensions(self):
        # .txt renders via the Chromium route now, not LibreOffice.
        self.assertNotIn(".txt", poller.OFFICE_EXT)
        self.assertIn(".csv", poller.OFFICE_EXT)

    def test_markup_extensions(self):
        for ext in (".html", ".htm", ".md", ".markdown", ".txt"):
            self.assertIn(ext, poller.MARKUP_EXT)


class MarkupTest(unittest.TestCase):
    def setUp(self):
        self.output_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.output_dir.cleanup)
        stub["post_handler"] = lambda url, files: _response(200, FAKE_PDF)
        http_calls[:] = []

    def _write_source(self, filename, data):
        source_path = os.path.join(self.output_dir.name, filename)
        with open(source_path, "wb") as source_file:
            source_file.write(data)
        return source_path

    def test_html_goes_through_as_is(self):
        body = b"<html><body><h1>Hi</h1></body></html>"
        output_path = poller.convert_markup(
            self._write_source("page.html", body), self.output_dir.name, ".html"
        )
        self.assertTrue(output_path.endswith("page.pdf"))
        self.assertEqual(http_calls, [(CHROMIUM_URL, "index.html")])
        self.assertEqual(last_upload["content"], body)

    def test_markdown_renders_to_html(self):
        output_path = poller.convert_markup(
            self._write_source("notes.md", b"# Title\n\nsome *text*"),
            self.output_dir.name,
            ".md",
        )
        self.assertTrue(output_path.endswith("notes.pdf"))
        self.assertEqual(http_calls, [(CHROMIUM_URL, "index.html")])
        self.assertIn(b"<h1>Title</h1>", last_upload["content"])

    def test_txt_wrapped_in_pre_escaped(self):
        output_path = poller.convert_markup(
            self._write_source("notes.txt", b"a <b> & c"),
            self.output_dir.name,
            ".txt",
        )
        self.assertTrue(output_path.endswith("notes.pdf"))
        self.assertEqual(http_calls, [(CHROMIUM_URL, "index.html")])
        self.assertIn(b"<pre>a &lt;b&gt; &amp; c</pre>", last_upload["content"])
        self.assertIn(b"<style>", last_upload["content"])


class RetryTest(unittest.TestCase):
    def setUp(self):
        self._orig_limit = poller.RETRY_LIMIT
        self._retries_dir = tempfile.mkdtemp()
        self._orig_retry_file = poller._RETRY_FILE
        poller._RETRY_FILE = os.path.join(self._retries_dir, "retries.json")
        self.addCleanup(setattr, poller, "RETRY_LIMIT", self._orig_limit)
        self.addCleanup(setattr, poller, "_RETRY_FILE", self._orig_retry_file)

        def _cleanup_retries_dir():
            rf = os.path.join(self._retries_dir, "retries.json")
            if os.path.exists(rf):
                os.unlink(rf)
            if os.path.exists(self._retries_dir):
                os.rmdir(self._retries_dir)

        self.addCleanup(_cleanup_retries_dir)

    def _build_message(self):
        msg = EmailMessage()
        msg["From"] = "a@example.com"
        msg["To"] = "print@example.com"
        msg["Subject"] = "test retry"
        msg["Message-ID"] = "<retry-test@example.com>"
        msg.set_content("body")
        return msg

    def test_retry_counter_increments(self):
        msg = self._build_message()
        key = poller._retry_key(msg)
        state = poller._load_retries()
        self.assertEqual(state, {})
        state[key] = {"count": 1, "ts": 1000.0}
        poller._save_retries(state)
        state = poller._load_retries()
        self.assertEqual(state[key]["count"], 1)

    def test_terminal_at_cap(self):
        poller.RETRY_LIMIT = 2
        msg = self._build_message()
        key = poller._retry_key(msg)

        stub["post_handler"] = lambda url, files: _response(200, FAKE_PDF)
        http_calls[:] = []

        fake_imap = FakeImap(msg.as_bytes())
        # First attempt — transient failure, stays in SOURCE_FOLDER
        with mock.patch.object(poller, "print_file", side_effect=poller.TransientError("lp down")):
            poller.process(fake_imap, "1")
        self.assertEqual(fake_imap.expunged, [])
        state = poller._load_retries()
        self.assertEqual(state[key]["count"], 1)

        # Second attempt — at cap, expunged
        fake_imap2 = FakeImap(msg.as_bytes())
        with mock.patch.object(poller, "print_file", side_effect=poller.TransientError("lp down")):
            poller.process(fake_imap2, "1")
        self.assertEqual(fake_imap2.expunged, ["1"])
        state = poller._load_retries()
        self.assertNotIn(key, state)

    def test_gc_removes_stale_entries(self):
        state = {
            "old": {"count": 1, "ts": 0.0},
            "new": {"count": 1, "ts": time.time()},
        }
        cleaned = poller._gc_retries(state)
        self.assertNotIn("old", cleaned)
        self.assertIn("new", cleaned)

    def test_retry_key_from_message_id(self):
        msg = self._build_message()
        key = poller._retry_key(msg)
        self.assertEqual(key, "<retry-test@example.com>")

    def test_retry_key_fallback_hash(self):
        msg = EmailMessage()
        msg["Date"] = "Mon, 1 Jan 2024 00:00:00 +0000"
        msg["Subject"] = "test"
        msg["From"] = "a@example.com"
        key = poller._retry_key(msg)
        self.assertTrue(key.startswith("h:"))


class DrainTest(unittest.TestCase):
    def _build_message(self, frm="a@example.com", to="print@example.com"):
        msg = EmailMessage()
        msg["From"] = frm
        msg["To"] = to
        msg["Subject"] = "t"
        msg["Message-ID"] = "<drain-test@example.com>"
        msg.set_content("body")
        return msg

    def test_stranger_dropped_headers_only(self):
        rejected_before = poller._state["rejected_total"]
        fake_imap = FakeImap(self._build_message(frm="stranger@evil.example").as_bytes())
        with mock.patch.object(poller.smtplib, "SMTP", FakeSmtp):
            sent_before = sum(len(i.sent_messages) for i in FakeSmtp.instances)
            poller.process(fake_imap, "1")
            sent_after = sum(len(i.sent_messages) for i in FakeSmtp.instances)
        # Body never downloaded, no reply, message expunged.
        self.assertEqual(fake_imap.fetched_headers, ["1"])
        self.assertEqual(fake_imap.fetched_full, [])
        self.assertEqual(sent_after, sent_before)
        self.assertEqual(fake_imap.expunged, ["1"])
        self.assertEqual(poller._state["rejected_total"], rejected_before + 1)

    def test_misaddressed_dropped(self):
        fake_imap = FakeImap(self._build_message(to="someone@else.example").as_bytes())
        poller.process(fake_imap, "1")
        self.assertEqual(fake_imap.fetched_full, [])
        self.assertEqual(fake_imap.expunged, ["1"])

    def test_auth_failure_deleted_silently(self):
        poller.REQUIRE_AUTH_PASS = True
        self.addCleanup(lambda: setattr(poller, "REQUIRE_AUTH_PASS", False))
        rejected_before = poller._state["rejected_total"]
        fake_imap = FakeImap(self._build_message().as_bytes())
        with mock.patch.object(poller.smtplib, "SMTP", FakeSmtp):
            sent_before = sum(len(i.sent_messages) for i in FakeSmtp.instances)
            poller.process(fake_imap, "1")
            sent_after = sum(len(i.sent_messages) for i in FakeSmtp.instances)
        self.assertEqual(sent_after, sent_before)
        self.assertEqual(fake_imap.expunged, ["1"])
        self.assertEqual(poller._state["rejected_total"], rejected_before + 1)

    def test_uidplus_guard(self):
        with self.assertRaises(SystemExit) as ctx:
            poller._require_uidplus(FakeImap(b"", capabilities=(b"IMAP4rev1",)))
        self.assertEqual(ctx.exception.code, 2)
        # UIDPLUS advertised — no exit.
        poller._require_uidplus(FakeImap(b""))

    def test_header_fetch_failure_logged_and_skipped(self):
        msg = self._build_message()
        fake_imap = FakeImap(msg.as_bytes())
        orig_uid = fake_imap.uid

        def failing_fetch(command, *args):
            if command == "FETCH" and "HEADER" in args[1]:
                return "NO", [b"fetch failed"]
            return orig_uid(command, *args)

        with mock.patch.object(fake_imap, "uid", side_effect=failing_fetch):
            with self.assertLogs(poller.log, level="WARNING") as captured:
                poller.process(fake_imap, "1")
        self.assertTrue(
            any("header fetch failed" in line for line in captured.output),
            captured.output,
        )
        self.assertEqual(fake_imap.fetched_full, [])
        self.assertEqual(fake_imap.expunged, [])

    def test_poll_empties_mailbox(self):
        retries_dir = tempfile.mkdtemp()
        orig_retry_file = poller._RETRY_FILE
        poller._RETRY_FILE = os.path.join(retries_dir, "retries.json")
        self.addCleanup(setattr, poller, "_RETRY_FILE", orig_retry_file)

        def _cleanup():
            rf = os.path.join(retries_dir, "retries.json")
            if os.path.exists(rf):
                os.unlink(rf)
            os.rmdir(retries_dir)

        self.addCleanup(_cleanup)
        stub["post_handler"] = lambda url, files: _response(200, FAKE_PDF)
        fake_imap = FakeImap(self._build_message().as_bytes())
        with mock.patch.object(poller, "print_file", return_value="ok"):
            poller.poll_once(fake_imap)
        self.assertEqual(fake_imap.selected, poller.SOURCE_FOLDER)
        self.assertEqual(fake_imap.expunged, ["1"])
        self.assertEqual(poller._state["pending_messages"], 0)


class VersionTest(unittest.TestCase):
    def setUp(self):
        orig_version = poller._state["gotenberg_version"]
        self.addCleanup(lambda: poller._state.__setitem__("gotenberg_version", orig_version))
        poller._state["gotenberg_version"] = None
        http_calls[:] = []

    def test_version_recorded(self):
        stub["get_handler"] = lambda url: _response(200, b"8.36.0")
        poller._check_gotenberg_version()
        self.assertEqual(poller._state["gotenberg_version"], "8.36.0")
        self.assertEqual(http_calls, [("http://gotenberg.test:3000/version", None)])

    def test_version_unreachable_leaves_unset(self):
        def raise_connection_error(url):
            raise _RequestException("gotenberg is down")

        stub["get_handler"] = raise_connection_error
        poller._check_gotenberg_version()  # must not raise
        self.assertIsNone(poller._state["gotenberg_version"])

    def test_version_bad_status_leaves_unset(self):
        stub["get_handler"] = lambda url: _response(500, b"boom")
        poller._check_gotenberg_version()
        self.assertIsNone(poller._state["gotenberg_version"])

    def test_version_unparseable_leaves_unset(self):
        stub["get_handler"] = lambda url: _response(200, b"not-a-version")
        poller._check_gotenberg_version()
        self.assertIsNone(poller._state["gotenberg_version"])


class HealthTest(unittest.TestCase):
    def test_health_includes_pending_messages(self):
        import io
        import json as _json

        poller._state["pending_messages"] = 7
        handler = poller._Health.__new__(poller._Health)
        handler.requestline = "GET /health HTTP/1.1"
        handler.path = "/health"
        handler.headers = {}
        handler.wfile = io.BytesIO()
        handler.request_version = "HTTP/1.1"

        class FakeSock:
            def __init__(self):
                self.data = b""

            def sendall(self, d):
                self.data += d

        handler.request = type("R", (), {"makefile": lambda *a, **kw: io.BytesIO()})()
        handler.connection = FakeSock()
        handler.close = lambda: None
        handler.address = ("127.0.0.1", 0)
        handler.setup = lambda: None

        # Patch send_response / send_header / end_headers to capture output
        responses = []
        headers = {}

        def fake_send_response(code):
            responses.append(code)

        def fake_send_header(k, v):
            headers[k] = v

        handler.send_response = fake_send_response
        handler.send_header = fake_send_header
        handler.end_headers = lambda: None
        handler.do_GET()

        body = handler.wfile.getvalue()
        data = _json.loads(body)
        self.assertEqual(data["pending_messages"], 7)
        self.assertIn("status", data)
        self.assertIn("uptime_s", data)

    def test_health_unknown_path_404(self):
        import io

        handler = poller._Health.__new__(poller._Health)
        handler.path = "/nope"
        responses = []
        handler.send_response = responses.append
        handler.end_headers = lambda: None
        handler.do_GET()
        self.assertEqual(responses, [404])


class AuthTest(unittest.TestCase):
    def _headers(self, results=None):
        msg = EmailMessage()
        msg["From"] = "a@example.com"
        msg["To"] = "print@example.com"
        if results is not None:
            msg["Authentication-Results"] = results
        return msg

    def test_auth_not_required(self):
        poller.REQUIRE_AUTH_PASS = False
        self.addCleanup(lambda: setattr(poller, "REQUIRE_AUTH_PASS", False))
        self.assertTrue(poller.auth_ok(self._headers()))

    def test_auth_spf_pass(self):
        poller.REQUIRE_AUTH_PASS = True
        self.addCleanup(lambda: setattr(poller, "REQUIRE_AUTH_PASS", False))
        self.assertTrue(
            poller.auth_ok(
                self._headers("mx.example; spf=pass smtp.mailfrom=a@example.com")
            )
        )

    def test_auth_dkim_pass(self):
        poller.REQUIRE_AUTH_PASS = True
        self.addCleanup(lambda: setattr(poller, "REQUIRE_AUTH_PASS", False))
        self.assertTrue(
            poller.auth_ok(self._headers("mx.example; dkim=pass header.d=example.com"))
        )

    def test_auth_missing_header_fails(self):
        poller.REQUIRE_AUTH_PASS = True
        self.addCleanup(lambda: setattr(poller, "REQUIRE_AUTH_PASS", False))
        self.assertFalse(poller.auth_ok(self._headers()))

    def test_auth_failed_values_fail(self):
        poller.REQUIRE_AUTH_PASS = True
        self.addCleanup(lambda: setattr(poller, "REQUIRE_AUTH_PASS", False))
        self.assertFalse(
            poller.auth_ok(self._headers("mx.example; spf=fail; dkim=fail"))
        )


class PrintFileTest(unittest.TestCase):
    def test_builds_lp_command(self):
        with mock.patch.object(poller.subprocess, "run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=0, stdout="req 1", stderr="")
            poller.print_file(
                "/tmp/01-x.pdf", {"sides": "one-sided", "media": "letter"}
            )
        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        self.assertEqual(
            cmd,
            [
                "lp",
                "-d",
                "TEST",
                "-o",
                "sides=one-sided",
                "-o",
                "media=letter",
                "--",
                "/tmp/01-x.pdf",
            ],
        )

    def test_nonzero_exit_is_transient(self):
        with mock.patch.object(poller.subprocess, "run") as run_mock:
            run_mock.return_value = mock.Mock(returncode=1, stdout="", stderr="no cups")
            with self.assertRaises(poller.TransientError) as ctx:
                poller.print_file("/tmp/01-x.pdf", {})
        self.assertIn("rc=1", str(ctx.exception))

    def test_missing_binary_is_transient(self):
        with mock.patch.object(poller.subprocess, "run") as run_mock:
            run_mock.side_effect = FileNotFoundError("lp")
            with self.assertRaises(poller.TransientError):
                poller.print_file("/tmp/01-x.pdf", {})

    def test_dry_run_skips_exec(self):
        poller.DRY_RUN = True
        self.addCleanup(lambda: setattr(poller, "DRY_RUN", False))

        def unexpected_call(*args, **kwargs):
            raise AssertionError("no lp exec expected in dry-run mode")

        with mock.patch.object(
            poller.subprocess, "run", side_effect=unexpected_call
        ) as run_mock:
            detail = poller.print_file("/tmp/01-x.pdf", {})
        self.assertTrue(detail)
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
