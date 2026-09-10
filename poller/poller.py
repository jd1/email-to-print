#!/usr/bin/env python3
"""print-poller: email-to-print bridge.

Watches the INBOX of a dedicated print account and drains it every cycle:
  - headers are fetched first; anything not addressed to PRINT_TO or from
    a non-allow-listed sender is logged (From/date/Subject) and expunged
    without ever downloading the body and without a reply;
  - allow-listed-looking senders failing SPF/DKIM (when REQUIRE_AUTH_PASS
    is on) also vanish silently;
  - everyone else gets their attachments printed: PDF/images straight to
    lp, Office documents via Gotenberg's LibreOffice route, HTML/markdown/
    text via its Chromium route, and body-only mail prints the rendered
    email body when PRINT_BODY=true.
Processing is two-phase: convert first, then print.  Transient failures
leave the message untouched for the next cycle, up to RETRY_LIMIT times;
terminal outcomes (printed, failed, dropped) are expunged by UID, so the
mailbox ends up empty.  Retry state persists in
$XDG_STATE_HOME/mailprint/retries.json and is GC'd after 7 days.
Requires a UIDPLUS-capable IMAP server (UID EXPUNGE).
Stdlib plus `requests`/`markdown`; external deps are `lp` (CUPS) and a
Gotenberg service.  Fail-closed on the allow-list.
SKIP_PRINT=true runs the real Gotenberg conversion but skips `lp`
(conversion test); DRY_RUN=true does neither.
Run with --once for a single poll cycle (exit 0 on success, 1 on failure);
without it the poller loops forever.
"""

import email
import html as _html
import imaplib
import json as _json
import logging
import mimetypes
import os
import smtplib
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, TypedDict, cast

import markdown
import requests


class TransientError(Exception):
    """Temporary failure — safe to retry on the next poll cycle."""


class PermanentError(Exception):
    """Non-retryable failure — expunge, do not retry."""


def env(k: str, d: Any = None, req: bool = False) -> Any:
    v = os.environ.get(k, d)
    if req and not v:
        logging.critical("missing required env %s", k)
        sys.exit(2)
    return v


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("print-poller")

IMAP_HOST = env("IMAP_HOST", "127.0.0.1")
IMAP_PORT = int(env("IMAP_PORT", "1143"))
IMAP_USER = env("IMAP_USER", req=True)
IMAP_PASS = env("IMAP_PASS", req=True)
SMTP_HOST = env("SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(env("SMTP_PORT", "1025"))
PRINT_TO = env("PRINT_TO", req=True).lower()
SOURCE_FOLDER = env("SOURCE_FOLDER", "INBOX")
ALLOWED = set(a.strip().lower() for a in env("ALLOWED_SENDERS", "").split(",") if a.strip())
PRINTER = env("PRINTER", req=True)
CUPS_SERVER = env("CUPS_SERVER", "127.0.0.1:631")
GOTENBERG_URL = env("GOTENBERG_URL", "http://127.0.0.1:3000").rstrip("/")
GOTENBERG_TIMEOUT = float(env("GOTENBERG_TIMEOUT", "120"))
POLL_INTERVAL = int(env("POLL_INTERVAL", "60"))
MAX_MB = float(env("MAX_ATTACH_MB", "25"))
CONFIRM_REPLY = env("CONFIRM_REPLY", "true").lower() == "true"
PRINT_BODY = env("PRINT_BODY", "true").lower() == "true"
DRY_RUN = env("DRY_RUN", "false").lower() == "true"
SKIP_PRINT = env("SKIP_PRINT", "false").lower() == "true"
REQUIRE_AUTH_PASS = env("REQUIRE_AUTH_PASS", "false").lower() == "true"
TLS_VERIFY = env("TLS_VERIFY", "true").lower() == "true"
IMAP_SSL = env("IMAP_SSL", "false").lower() == "true"
RETRY_LIMIT = int(env("RETRY_LIMIT", "3"))
PRINT_OPTS = {"sides": env("SIDES", "one-sided"), "media": env("MEDIA", "letter")}
LP_TIMEOUT = float(env("LP_TIMEOUT", "120"))
HEALTH_BIND = env("HEALTH_BIND", "127.0.0.1")

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
if not TLS_VERIFY and not (IMAP_HOST in _LOCAL_HOSTS and SMTP_HOST in _LOCAL_HOSTS):
    logging.critical(
        "TLS_VERIFY=false is only allowed for localhost (bridge). IMAP_HOST=%s SMTP_HOST=%s",
        IMAP_HOST,
        SMTP_HOST,
    )
    sys.exit(2)

os.environ["CUPS_SERVER"] = CUPS_SERVER
HEALTH_PORT = int(env("HEALTH_PORT", "2631"))
_state: dict[str, Any] = {
    "started": time.time(),
    "last_poll": None,
    "last_poll_ok": False,
    "printed_total": 0,
    "rejected_total": 0,
    "errors_total": 0,
    "pending_messages": 0,
    "gotenberg_version": None,
}


def _default_state_dir() -> str:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return os.path.join(xdg, "mailprint")
    return os.path.join(os.path.expanduser("~"), ".local", "state", "mailprint")


_RETRY_DIR = _default_state_dir()
_RETRY_FILE = os.path.join(_RETRY_DIR, "retries.json")
_RETRY_GC_DAYS = 7


class RetryEntry(TypedDict, total=False):
    count: int
    ts: float


def _load_retries() -> dict[str, RetryEntry]:
    try:
        with open(_RETRY_FILE) as f:
            loaded: Any = _json.load(f)
    except (OSError, ValueError):
        # Missing/unreadable file, or corrupt JSON (JSONDecodeError is a
        # ValueError). Reset to empty rather than failing the poll cycle.
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {k: cast("RetryEntry", v) for k, v in loaded.items() if isinstance(v, dict)}


def _save_retries(state: dict[str, RetryEntry]) -> None:
    os.makedirs(_RETRY_DIR, exist_ok=True, mode=0o700)
    tmp = _RETRY_FILE + ".tmp"
    with open(tmp, "w") as f:
        _json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, _RETRY_FILE)


def _retry_key(msg: Any) -> str:
    """Generate a stable retry key from message headers."""
    mid: Any = msg.get("Message-ID", "")
    if mid:
        return str(mid)
    # Fallback: hash Date + Subject + From
    parts = [
        msg.get("Date", ""),
        msg.get("Subject", ""),
        msg.get("From", ""),
    ]
    import hashlib

    return "h:" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _gc_retries(state: dict[str, RetryEntry]) -> dict[str, RetryEntry]:
    """Remove retry entries older than _RETRY_GC_DAYS."""
    cutoff = time.time() - (_RETRY_GC_DAYS * 86400)
    return {k: v for k, v in state.items() if v.get("ts", 0) >= cutoff}


class _Health(BaseHTTPRequestHandler):
    def log_message(self, *a: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.rstrip("/") not in ("/health", ""):
            self.send_response(404)
            self.end_headers()
            return
        ok = _state["last_poll_ok"] and _state["last_poll"] is not None
        status = "ok" if ok else ("starting" if _state["last_poll"] is None else "degraded")
        lp = _state["last_poll"]
        body = _json.dumps(
            {
                "status": status,
                "printer": PRINTER,
                "source": SOURCE_FOLDER,
                "last_poll": (
                    None if lp is None else time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(lp))
                ),
                "printed_total": _state["printed_total"],
                "rejected_total": _state["rejected_total"],
                "errors_total": _state["errors_total"],
                "pending_messages": _state["pending_messages"],
                "gotenberg_version": _state["gotenberg_version"],
                "uptime_s": int(time.time() - _state["started"]),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_health() -> None:
    try:
        HTTPServer((HEALTH_BIND, HEALTH_PORT), _Health).serve_forever()
    except Exception as e:
        log.warning("health server failed: %s", e)


def _check_gotenberg_version() -> None:
    """Record the Gotenberg server version once at startup.

    The poller only uses convert paths that are identical across v8
    minors, but a major-version jump may change options — logging the
    server version up front (including on DRY_RUN passes, which skip
    all conversions) makes that visible before anything prints.
    Never fatal: an unreachable Gotenberg just leaves the version unset.
    """
    try:
        response = requests.get(GOTENBERG_URL + "/version", timeout=10)
    except requests.exceptions.RequestException as e:
        log.warning("could not read Gotenberg version: %s", e)
        return
    if response.status_code != 200:
        log.warning("Gotenberg /version returned HTTP %s", response.status_code)
        return
    version = response.text.strip()
    major = version.split(".")[0] if version[:1].isdigit() else ""
    if not major:
        log.warning("unparseable Gotenberg version: %r", response.text[:50])
        return
    _state["gotenberg_version"] = version
    log.info("Gotenberg server version %s (major %s)", version, major)


NATIVE_CTYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/tiff",
}
NATIVE_EXT = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
# .txt used to go through LibreOffice; it now renders via Chromium
# (see MARKUP_EXT below).
OFFICE_EXT = {
    ".doc",
    ".docx",
    ".odt",
    ".rtf",
    ".xls",
    ".xlsx",
    ".ods",
    ".ppt",
    ".pptx",
    ".odp",
    ".csv",
}
# Attachments rendered to PDF through Gotenberg's Chromium route.
MARKUP_EXT = {".html", ".htm", ".md", ".markdown", ".txt"}
# Minimal print stylesheet wrapping converted markup and plain-text bodies.
PRINT_CSS = (
    "@page{margin:18mm} body{font-family:sans-serif;font-size:11pt;"
    "word-wrap:break-word} pre{white-space:pre-wrap;font-size:10pt}"
)


def dh(s: Any) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception as e:
        log.debug("header decode failed, using raw value: %s", e)
        return s or ""


def addrs(msg: Any, *headers: str) -> list[str]:
    vals: list[str] = []
    for h in headers:
        vals += msg.get_all(h, [])
    return [a.lower() for _, a in getaddresses(vals) if a]


def _tls_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not TLS_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def imap_connect() -> Any:
    M: Any
    if IMAP_SSL:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=_tls_ctx())
    else:
        M = imaplib.IMAP4(IMAP_HOST, IMAP_PORT)
        M.starttls(ssl_context=_tls_ctx())
    M.login(IMAP_USER, IMAP_PASS)
    return M


# Headers needed to route a message before its body is worth downloading.
HDR_FIELDS = (
    "From",
    "To",
    "Cc",
    "Delivered-To",
    "X-Original-To",
    "X-Forwarded-To",
    "Subject",
    "Date",
    "Message-ID",
    "Authentication-Results",
)


def _uid_str(uid: Any) -> str:
    """Decode a UID from IMAP responses (bytes from SEARCH) for logging."""
    return uid.decode("ascii", errors="replace") if isinstance(uid, bytes) else str(uid)


def fetch_headers(M: Any, uid: Any) -> Any:
    """Fetch only routing headers; BODY.PEEK leaves \\Seen unset."""
    fields = " ".join(HDR_FIELDS)
    typ, data = M.uid("FETCH", uid, f"(BODY.PEEK[HEADER.FIELDS ({fields})])")
    if typ != "OK" or not data or not data[0]:
        log.warning("header fetch failed uid=%s type=%s (skipped, will retry)", _uid_str(uid), typ)
        return None
    return email.message_from_bytes(data[0][1])


def delete_uid(M: Any, uid: Any) -> None:
    """Mark \\Deleted and expunge just this UID (requires UIDPLUS)."""
    typ, _ = M.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
    if typ != "OK":
        raise TransientError(f"STORE \\Deleted uid={_uid_str(uid)} failed")
    typ, _ = M.uid("EXPUNGE", uid)
    if typ != "OK":
        raise TransientError(f"EXPUNGE uid={_uid_str(uid)} failed")


def stranger_drop(M: Any, uid: Any, msg: Any, why: str) -> None:
    """Log and silently expunge a message that must never print."""
    log.warning(
        "DROP %s: from=%s date=%s subj=%r",
        why,
        parseaddr(msg.get("From", ""))[1],
        msg.get("Date", ""),
        dh(msg.get("Subject", "(no subject)")),
    )
    delete_uid(M, uid)
    _state["rejected_total"] += 1


def _server_capabilities(M: Any) -> set[str]:
    try:
        typ, data = M.capability()
    except Exception as e:
        log.debug("capability check failed: %s", e)
        return set()
    if typ != "OK" or not data:
        return set()
    caps = set()
    for chunk in data:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("ascii", errors="replace")
        caps.update(str(chunk).upper().split())
    return caps


def _require_uidplus(M: Any) -> None:
    if "UIDPLUS" not in _server_capabilities(M):
        logging.critical(
            "IMAP server does not advertise UIDPLUS (UID EXPUNGE); "
            "the empty-mailbox guarantee needs it. Refusing to start."
        )
        sys.exit(2)


def print_file(path: str, opts: dict[str, str]) -> str:
    """Submit one PDF to CUPS; return the lp output detail string.

    Raises TransientError when the job was not accepted (safe to retry).
    """
    cmd = ["lp", "-d", PRINTER]
    for k, v in opts.items():
        cmd += ["-o", f"{k}={v}"]
    cmd += ["--", path]
    log.info("printing %s (%s)", os.path.basename(path), " ".join(cmd))
    if DRY_RUN:
        return "dry-run"
    if SKIP_PRINT:
        log.info("[skip-print] %s converted, not submitting to CUPS", os.path.basename(path))
        return "skip-print"
    try:
        # No shell; cmd is built from config + a converted temp file path.
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=LP_TIMEOUT)  # noqa: S603
        if r.returncode != 0:
            raise TransientError(f"lp failed (rc={r.returncode}): {r.stdout + r.stderr}".strip())
        return (r.stdout + r.stderr).strip()
    except TransientError:
        raise
    except Exception as e:
        raise TransientError(f"lp error: {e}") from e


def _gotenberg_post(route: str, filename: str, content: bytes) -> bytes:
    """POST one file to Gotenberg; return PDF bytes.

    Raises TransientError on connection/timeout/server errors (safe to retry)
    and PermanentError on client errors or non-PDF responses.
    """
    if DRY_RUN:
        log.info(
            "[dry-run] would POST %s%s (%s, %d bytes)",
            GOTENBERG_URL,
            route,
            filename,
            len(content),
        )
        return b""
    try:
        response = requests.post(
            GOTENBERG_URL + route,
            files={"files": (filename, content)},
            timeout=GOTENBERG_TIMEOUT,
        )
    except requests.exceptions.RequestException as error:
        log.error("gotenberg unreachable for %s: %s", filename, error)
        raise TransientError(f"gotenberg unreachable for {filename}: {error}") from error
    if response.status_code >= 500:
        log.error(
            "gotenberg %s server error for %s: HTTP %s",
            route,
            filename,
            response.status_code,
        )
        raise TransientError(
            f"gotenberg {route} server error for {filename}: HTTP {response.status_code}"
        )
    if response.status_code != 200 or not response.content.startswith(b"%PDF"):
        log.error(
            "gotenberg %s failed for %s: HTTP %s %s",
            route,
            filename,
            response.status_code,
            response.text[:200],
        )
        raise PermanentError(
            f"gotenberg {route} failed for {filename}: HTTP {response.status_code}"
        )
    return response.content


def to_pdf(source_path: str, output_dir: str) -> str:
    """Office doc -> PDF via Gotenberg's LibreOffice route.

    Propagates TransientError/PermanentError from Gotenberg.
    """
    with open(source_path, "rb") as source_file:
        source_bytes = source_file.read()
    pdf_bytes = _gotenberg_post(
        "/forms/libreoffice/convert", os.path.basename(source_path), source_bytes
    )
    output_path = os.path.join(
        output_dir, os.path.splitext(os.path.basename(source_path))[0] + ".pdf"
    )
    with open(output_path, "wb") as output_file:
        output_file.write(pdf_bytes)
    return output_path


def convert_markup(source_path: str, output_dir: str, ext: str) -> str:
    """HTML/markdown/text file -> PDF via Gotenberg's Chromium route.

    `.html`/`.htm` go through as-is; `.md`/`.markdown` render via
    python-markdown; anything else (`.txt`) is wrapped in `<pre>` with
    print CSS. Propagates TransientError/PermanentError from Gotenberg.
    """
    if ext in (".html", ".htm"):
        with open(source_path, "rb") as source_file:
            page_bytes = source_file.read()
    else:
        with open(source_path, encoding="utf-8", errors="replace") as source_file:
            text = source_file.read()
        if ext in (".md", ".markdown"):
            inner = markdown.markdown(text, extensions=["extra"])
        else:
            inner = "<pre>" + _html.escape(text) + "</pre>"
        page_bytes = (
            "<html><head><meta charset='utf-8'><style>"
            + PRINT_CSS
            + "</style></head><body>"
            + inner
            + "</body></html>"
        ).encode()
    return convert_html(
        page_bytes, output_dir, os.path.splitext(os.path.basename(source_path))[0] or "doc"
    )


def convert_html(html_bytes: bytes, output_dir: str, name_stem: str) -> str:
    """HTML -> PDF via Gotenberg's Chromium route (upload must be index.html).

    Propagates TransientError/PermanentError from Gotenberg.
    """
    pdf_bytes = _gotenberg_post("/forms/chromium/convert/html", "index.html", html_bytes)
    output_path = os.path.join(output_dir, name_stem + ".pdf")
    with open(output_path, "wb") as output_file:
        output_file.write(pdf_bytes)
    return output_path


def render_body(msg: Any, output_dir: str) -> str | None:
    html_bytes = text_bytes = None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if "attachment" in (part.get("Content-Disposition") or "").lower():
            continue
        content_type = part.get_content_type()
        if content_type == "text/html" and html_bytes is None:
            html_bytes = part.get_payload(decode=True)
        elif content_type == "text/plain" and text_bytes is None:
            text_bytes = part.get_payload(decode=True)
    if html_bytes:
        return convert_html(html_bytes, output_dir, "email-body")
    if text_bytes:
        escaped_body = (
            "<pre>" + _html.escape(text_bytes.decode("utf-8", errors="replace")) + "</pre>"
        )
        page_bytes = (
            "<html><head><meta charset='utf-8'></head><body>" + escaped_body + "</body></html>"
        ).encode()
        return convert_html(page_bytes, output_dir, "email-body")
    return None


def _print_label() -> str:
    return "Skipped printing: " if SKIP_PRINT else "Queued for printing: "


def auth_ok(msg: Any) -> bool:
    if not REQUIRE_AUTH_PASS:
        return True
    ar = " ".join(msg.get_all("Authentication-Results", [])).lower()
    return "spf=pass" in ar or "dkim=pass" in ar


def send_reply(orig: Any, to_addr: str, status: str, detail: str) -> None:
    if not CONFIRM_REPLY or not to_addr:
        return
    try:
        m = EmailMessage()
        m["From"] = IMAP_USER
        m["To"] = to_addr
        m["Subject"] = f"Print {status}: " + dh(orig.get("Subject", "(no subject)"))
        if orig.get("Message-ID"):
            m["In-Reply-To"] = orig["Message-ID"]
        m["Date"] = formatdate(localtime=True)
        m["Message-ID"] = make_msgid()
        m.set_content(detail)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls(context=_tls_ctx())
            s.login(IMAP_USER, IMAP_PASS)
            s.send_message(m)
    except Exception as e:
        log.warning("confirm reply failed: %s", e)


def process(M: Any, uid: Any) -> None:
    # Phase 0: headers only — strangers never cost more than a few KB.
    hdr = fetch_headers(M, uid)
    if hdr is None:
        return
    if PRINT_TO not in addrs(hdr, "To", "Cc", "Delivered-To", "X-Original-To", "X-Forwarded-To"):
        stranger_drop(M, uid, hdr, f"not addressed to {PRINT_TO}")
        return
    frm = parseaddr(hdr.get("From", ""))[1].lower()
    if (not ALLOWED) or (frm not in ALLOWED):
        stranger_drop(M, uid, hdr, "sender not allowed")
        return
    if not auth_ok(hdr):
        log.warning("REJECT failed SPF/DKIM (silent): %s", frm)
        delete_uid(M, uid)
        _state["rejected_total"] += 1
        return
    subj = dh(hdr.get("Subject", "(no subject)"))
    log.info("candidate from=%s subj=%r", frm, subj)
    typ, data = M.uid("FETCH", uid, "(RFC822)")
    if typ != "OK" or not data or not data[0]:
        return
    msg = email.message_from_bytes(data[0][1])
    printed: list[str] = []
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as wd:
        # Phase 1: convert attachments and collect print jobs.
        jobs: list[tuple[str, str]] = []
        idx = 0
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            fn = dh(part.get_filename() or "")
            disp = (part.get("Content-Disposition") or "").lower()
            ctype = (part.get_content_type() or "").lower()
            if not fn and "attachment" not in disp:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if not isinstance(payload, (bytes, bytearray)):
                errors.append(f"{fn or ctype}: unreadable payload")
                continue
            if len(payload) > MAX_MB * 1024 * 1024:
                errors.append(f"{fn or ctype}: exceeds {MAX_MB}MB")
                continue
            ext = os.path.splitext(fn)[1].lower()
            raw = (
                os.path.basename(fn)
                if fn
                else "attachment" + (mimetypes.guess_extension(ctype) or ".bin")
            )
            clean = (
                "".join(c if c.isalnum() or c in "._- " else "_" for c in raw)[-120:].strip(". ")
                or "attachment.bin"
            )
            idx += 1
            safe = os.path.join(wd, f"{idx:02d}-{clean}")
            with open(safe, "wb") as f:
                f.write(payload)
            target: str
            if ctype in NATIVE_CTYPES or ext in NATIVE_EXT:
                target = safe
            elif ext in OFFICE_EXT:
                try:
                    target = to_pdf(safe, wd)
                except (TransientError, PermanentError) as exc:
                    errors.append(f"{fn}: {exc}")
                    continue
            elif ext in MARKUP_EXT:
                try:
                    target = convert_markup(safe, wd, ext)
                except (TransientError, PermanentError) as exc:
                    errors.append(f"{fn}: {exc}")
                    continue
            else:
                log.info("skip unsupported attachment %s (%s)", fn, ctype)
                continue
            jobs.append((target, fn))
        # No printable attachment -> print the email body itself (forward-to-print)
        if not jobs and not errors and PRINT_BODY:
            try:
                body_pdf = render_body(msg, wd)
            except (TransientError, PermanentError) as exc:
                errors.append(str(exc))
                body_pdf = None
            if body_pdf:
                jobs.append((body_pdf, "email body"))
        if not jobs and not errors:
            # Either PRINT_BODY is off or the body rendered to nothing: say so
            # explicitly instead of failing with a blank error detail.
            errors.append("no printable attachment and no renderable body")
        # Phase 2: submit all converted jobs to CUPS.
        try:
            for path, label in jobs:
                print_file(path, PRINT_OPTS)
                printed.append(label)
        except TransientError as exc:
            retries = _gc_retries(_load_retries())
            key = _retry_key(msg)
            entry: RetryEntry = retries.get(key, {"count": 0, "ts": time.time()})
            entry["count"] = entry.get("count", 0) + 1
            entry["ts"] = time.time()
            retries[key] = entry
            _save_retries(retries)
            if entry["count"] >= RETRY_LIMIT:
                log.warning(
                    "RETRY cap uid=%s key=%s attempts=%d",
                    uid,
                    key,
                    entry["count"],
                )
                delete_uid(M, uid)
                send_reply(
                    msg,
                    frm,
                    "failed",
                    f"Print failed after {entry['count']} attempts: {exc}",
                )
                _state["rejected_total"] += 1
                del retries[key]
                _save_retries(retries)
            else:
                log.warning(
                    "transient uid=%s key=%s attempt=%d/%d: %s",
                    uid,
                    key,
                    entry["count"],
                    RETRY_LIMIT,
                    exc,
                )
                _state["errors_total"] += 1
            return
    # Success — clear any prior retry state for this message.
    retries = _load_retries()
    key = _retry_key(msg)
    if key in retries:
        del retries[key]
        _save_retries(retries)
    # Unconditional exit: every terminal outcome expunges the message, so
    # the mailbox drains and a failed reply can never cause a reprint loop.
    if printed and not errors:
        delete_uid(M, uid)
        send_reply(msg, frm, "queued", _print_label() + ", ".join(printed))
        _state["printed_total"] += 1
        log.info("DONE printed=%s", printed)
    elif printed:
        delete_uid(M, uid)
        send_reply(
            msg,
            frm,
            "partial",
            _print_label() + ", ".join(printed) + "\nErrors: " + "; ".join(errors),
        )
        log.warning("PARTIAL printed=%s errors=%s", printed, errors)
    else:
        delete_uid(M, uid)
        send_reply(msg, frm, "failed", "Nothing could be printed. " + "; ".join(errors))
        _state["rejected_total"] += 1
        log.warning("NO-PRINT errors=%s", errors)


def poll_once(M: Any) -> None:
    # Expire stale retry entries every cycle so the state file cannot grow
    # unboundedly when no transient failure triggers a GC.
    retries = _load_retries()
    cleaned = _gc_retries(retries)
    if len(cleaned) != len(retries):
        _save_retries(cleaned)
    M.select(SOURCE_FOLDER)
    # Everything in the dedicated folder is a candidate; process() filters on
    # PRINT_TO. (Header SEARCH missed Cc/X-Original-To-only routing.)
    typ, data = M.uid("SEARCH", None, "ALL")
    uids = data[0].split() if typ == "OK" and data and data[0] else []
    if uids:
        log.info("%d candidate message(s) in %s", len(uids), SOURCE_FOLDER)
    for uid in uids:
        try:
            process(M, uid)
        except Exception as e:
            _state["errors_total"] += 1
            log.exception("error on uid=%s: %s", uid, e)
    # Recount remaining messages (retry cases stay in SOURCE_FOLDER).
    try:
        typ2, data2 = M.uid("SEARCH", None, "ALL")
        _state["pending_messages"] = (
            len(data2[0].split()) if typ2 == "OK" and data2 and data2[0] else 0
        )
    except Exception as e:
        log.debug("pending recount failed: %s", e)
    _state["last_poll"] = time.time()
    _state["last_poll_ok"] = True


def main(once: bool = False) -> None:
    startup_msg = (
        "print-poller up: user=%s source=%s printer=%s match=%s "
        "print_body=%s allow=%s dry=%s skip_print=%s once=%s"
    )
    log.info(
        startup_msg,
        IMAP_USER,
        SOURCE_FOLDER,
        PRINTER,
        PRINT_TO,
        PRINT_BODY,
        sorted(ALLOWED) or "(NONE-fail-closed)",
        DRY_RUN,
        SKIP_PRINT,
        once,
    )
    if not ALLOWED:
        log.warning("ALLOWED_SENDERS empty -> fail-closed")
    threading.Thread(target=_start_health, daemon=True).start()
    log.info("health endpoint on :%d/health", HEALTH_PORT)
    _check_gotenberg_version()
    M = imap_connect()
    try:
        _require_uidplus(M)
    finally:
        try:
            M.logout()
        except Exception as e:
            log.debug("IMAP logout failed: %s", e)
    while True:
        ok = False
        try:
            M = imap_connect()
            try:
                poll_once(M)
            finally:
                try:
                    M.logout()
                except Exception as e:
                    log.debug("IMAP logout failed: %s", e)
            ok = True
        except Exception as e:
            _state["last_poll_ok"] = False
            log.exception("poll cycle failed: %s", e)
        if once:
            sys.exit(0 if ok else 1)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main(once="--once" in sys.argv[1:])
