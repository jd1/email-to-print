"""Startup guards: fail fast with exit 2 before touching the network.

These tests don't need the mox fixture at all - the poller must refuse to
start before any IMAP/SMTP connection is attempted.
"""


def test_missing_required_env_exits_2(make_env, run_poller):
    env = make_env()
    del env["IMAP_USER"]
    r = run_poller(env)
    assert r.returncode == 2
    assert "missing required env IMAP_USER" in r.stderr


def test_missing_printer_exits_2(make_env, run_poller):
    env = make_env()
    del env["PRINTER"]
    r = run_poller(env)
    assert r.returncode == 2
    assert "missing required env PRINTER" in r.stderr


def test_tls_verify_false_non_localhost_exits_2(make_env, run_poller):
    env = make_env(IMAP_HOST="mail.example.com", SMTP_HOST="mail.example.com")
    r = run_poller(env)
    assert r.returncode == 2
    assert "TLS_VERIFY=false is only allowed for localhost" in r.stderr
