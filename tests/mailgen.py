"""Builders for test mail and attachments (no external deps, no committed binaries)."""

import base64
import io
import zipfile
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

MINIMAL_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]
          /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length 44 >> stream
BT /F1 24 Tf 72 720 Td (test page) Tj ET
endstream endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
xref
0 6
0000000000 65535 f 
trailer << /Size 6 /Root 1 0 R >>
startxref
0
%%EOF
"""

# 1x1 red pixel
MINIMAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


def make_docx(text="test document"):
    """Minimal valid .docx (single paragraph). Real LibreOffice opens it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>",
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def make_eml(
    from_addr,
    to_addr,
    subject,
    text=None,
    html=None,
    attachments=(),
    extra_headers=None,
    cc=None,
):
    """Build a raw .eml. attachments: list of (filename, bytes, "type/subtype").

    extra_headers: dict of raw headers added verbatim (e.g. Authentication-Results,
    Delivered-To, X-Original-To) - used to simulate provider-stamped headers.
    """
    m = EmailMessage()
    m["From"] = from_addr
    m["To"] = to_addr
    if cc:
        m["Cc"] = cc
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain="localhost")
    if text is not None or html is not None:
        m.set_content(text if text is not None else "")
        if html is not None:
            m.add_alternative(html, subtype="html")
    for fn, data, ctype in attachments:
        main, sub = ctype.split("/", 1)
        m.add_attachment(data, maintype=main, subtype=sub, filename=fn)
    for k, v in (extra_headers or {}).items():
        m[k] = v
    return m.as_bytes()
