#!/usr/bin/env python3
"""Unit tests for the Gotenberg conversion path. No IMAP/SMTP/network:
`requests` is stubbed before poller is imported, `lp` is mocked per-test."""
import os, sys, email, tempfile, types, unittest
from email.message import EmailMessage
from unittest import mock

os.environ.update({
    "IMAP_USER": "u@example.com", "IMAP_PASS": "x", "PRINT_TO": "print@example.com",
    "PRINTER": "TEST", "ALLOWED_SENDERS": "a@example.com", "CONFIRM_REPLY": "false",
    "DRY_RUN": "false", "GOTENBERG_URL": "http://gotenberg.test:3000",
    "GOTENBERG_TIMEOUT": "5",
})

class _RequestException(Exception): pass
_exceptions_module = types.ModuleType("requests.exceptions")
_exceptions_module.RequestException = _RequestException
_fake_requests_module = types.ModuleType("requests")
_fake_requests_module.exceptions = _exceptions_module

http_calls = []
last_upload = {"filename": None, "content": None}
stub = {"post_handler": None}

def _fake_post(url, files=None, timeout=None):
    upload_name, upload_content = (files or {}).get("files", (None, None))
    http_calls.append((url, upload_name))
    last_upload["filename"] = upload_name
    last_upload["content"] = upload_content
    return stub["post_handler"](url, files)

_fake_requests_module.post = _fake_post
sys.modules.setdefault("requests", _fake_requests_module)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "poller"))
import poller

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
        with open(source_path, "wb") as source_file: source_file.write(data)
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
        self.assertIsNone(poller.to_pdf(self._write_source("a.docx"), self.output_dir.name))

    def test_office_http_500(self):
        stub["post_handler"] = lambda url, files: _response(500, b"boom")
        self.assertIsNone(poller.to_pdf(self._write_source("a.docx"), self.output_dir.name))

    def test_office_http_422(self):
        stub["post_handler"] = lambda url, files: _response(422, b"invalid document")
        self.assertIsNone(poller.to_pdf(self._write_source("a.docx"), self.output_dir.name))

    def test_office_non_pdf_response_body(self):
        stub["post_handler"] = lambda url, files: _response(200, b"not a pdf")
        self.assertIsNone(poller.to_pdf(self._write_source("a.docx"), self.output_dir.name))

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
            "--b\r\nContent-Type: text/html\r\n\r\n<h1>Hi &amp; bye</h1>\r\n--b--\r\n")
        output_path = poller.render_body(msg, self.output_dir.name)
        self.assertTrue(output_path and output_path.endswith("email-body.pdf"))
        self.assertEqual(http_calls, [(CHROMIUM_URL, "index.html")])
        self.assertIn(b"<h1>Hi", last_upload["content"])

    def test_text_body_escaped(self):
        stub["post_handler"], http_calls[:] = self.ok_handler, []
        msg = email.message_from_string(
            "From: a@example.com\r\nTo: print@example.com\r\n"
            "Content-Type: text/plain\r\n\r\nHello <b>& friends\r\n")
        output_path = poller.render_body(msg, self.output_dir.name)
        self.assertTrue(output_path)
        self.assertEqual(http_calls, [(CHROMIUM_URL, "index.html")])
        self.assertIn(b"<pre>Hello &lt;b&gt;&amp; friends", last_upload["content"])

    def test_no_body(self):
        def unexpected_call(url, files):
            raise AssertionError("no body, so no http expected")
        stub["post_handler"] = unexpected_call
        self.assertIsNone(poller.render_body(email.message_from_string("Subject: x\r\n"),
                                             self.output_dir.name))


class RoutingTest(unittest.TestCase):
    def _build_message(self, attachments):
        msg = EmailMessage()
        msg["From"] = "a@example.com"; msg["To"] = "print@example.com"; msg["Subject"] = "t"
        msg.set_content("a body")
        for filename, content_type, data in attachments:
            maintype, subtype = content_type.split("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        return msg

    def test_docx_converted_txt_skipped(self):
        stub["post_handler"] = lambda url, files: _response(200, FAKE_PDF)
        http_calls[:] = []
        msg = self._build_message([
            ("rep.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"x"),
            ("notes.txt", "text/plain", b"hi"),
        ])

        class FakeImap:
            def __init__(self, raw_bytes):
                self.raw_bytes = raw_bytes
                self.moved_uids = []
            def uid(self, command, *args):
                if command == "FETCH":
                    return "OK", [(b"1 (RFC822)", self.raw_bytes)]
                if command == "MOVE":
                    self.moved_uids.append(args)
                return "OK", []

        fake_imap = FakeImap(msg.as_bytes())
        with mock.patch.object(poller, "print_file", return_value=(True, "ok")) as print_mock:
            poller.process(fake_imap, "1")
        self.assertEqual(http_calls, [(LIBREOFFICE_URL, "01-rep.docx")])  # .txt skipped
        self.assertEqual(print_mock.call_count, 1)                        # only converted docx
        self.assertEqual(fake_imap.moved_uids, [("1", poller.PROCESSED_FOLDER)])


class ExtensionTest(unittest.TestCase):
    def test_office_extensions(self):
        # .txt used to be converted by the bundled LibreOffice; skipped now.
        self.assertNotIn(".txt", poller.OFFICE_EXT)
        self.assertIn(".csv", poller.OFFICE_EXT)


if __name__ == "__main__":
    unittest.main()