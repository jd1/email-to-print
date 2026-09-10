# Local e2e suite: a real mox mail server on localhost, a fake Gotenberg
# service, fake lp on PATH, and the poller itself as a subprocess.
# No docker, no network beyond localhost.
# Python deps: poller/requirements.txt + tests/requirements_test.txt.
#
# MOX_VERSION has a single source: MOX_VERSION in tests/conftest.py.
MOX_VERSION := $(shell sed -n 's/^MOX_VERSION = "\(.*\)"/\1/p' tests/conftest.py)
MOX_URL := https://beta.gobuilds.org/github.com/mjl-/mox@$(MOX_VERSION)/linux-amd64-latest/

.PHONY: venv mox test test-slow clean

venv:
	python3 -m venv .venv
	.venv/bin/pip install -r poller/requirements.txt -r tests/requirements_test.txt

# `go install` is GOSUMDB-verified, so it wins where Go exists; the gobuilds
# mirror is only for machines without Go (and hard-fails if it serves a
# build page instead of a binary).
mox:
	mkdir -p .tools
	if command -v go >/dev/null; then \
		GOBIN=$(CURDIR)/.tools go install github.com/mjl-/mox@$(MOX_VERSION); \
	else \
		curl -sSL "$(MOX_URL)" -o .tools/mox; \
		chmod +x .tools/mox; \
		head -c 4 .tools/mox | grep -q ELF || (echo "mox download is not a binary" >&2; exit 1); \
	fi

test: venv mox
	.venv/bin/python -m pytest -m "not slow"

test-slow: venv mox
	.venv/bin/python -m pytest

clean:
	rm -rf .venv
