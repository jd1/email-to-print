# Local e2e suite: a real mox mail server on localhost, fake lp/soffice on
# PATH, and the poller itself as a subprocess. No docker, no network.
# Python deps: pytest only.
#
# Keep MOX_VERSION in sync with MOX_VERSION in tests/conftest.py.
MOX_VERSION := v0.0.17
MOX_URL := https://beta.gobuilds.org/github.com/mjl-/mox@$(MOX_VERSION)/linux-amd64-latest/

.PHONY: venv mox test test-slow clean

venv:
	python3 -m venv .venv
	.venv/bin/pip install -r poller/requirements.txt -r tests/requirements_test.txt

mox:
	mkdir -p .tools
	curl -sSL "$(MOX_URL)" -o .tools/mox
	chmod +x .tools/mox
	@if ! head -c 4 .tools/mox | grep -q ELF; then \
		echo "gobuilds served a build page instead of a binary; falling back to 'go install'..."; \
		rm -f .tools/mox; \
		GOBIN=$(CURDIR)/.tools go install github.com/mjl-/mox@$(MOX_VERSION); \
	fi

test: venv mox
	.venv/bin/python -m pytest -m "not slow"

test-slow: venv mox
	.venv/bin/python -m pytest

clean:
	rm -rf .venv
