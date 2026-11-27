PYTHON ?= python3
NODE ?= node
DOCKER ?= docker
PREFIX ?= $(HOME)/.local

.PHONY: all build install check test coverage images image-worker image-qa image-runtime run doctor smoke

all: build images

build: check
	$(PYTHON) -m aegis.build

install: build
	install -d "$(DESTDIR)$(PREFIX)/bin"
	@set -eu; \
	  target="$(DESTDIR)$(PREFIX)/bin/aegis"; \
	  test ! -d "$$target"; \
	  tmp=$$(mktemp "$$target.XXXXXX"); \
	  trap 'rm -f "$$tmp"' EXIT; \
	  install -m 0755 dist/aegis "$$tmp"; \
	  mv -f "$$tmp" "$$target"; \
	  printf 'Installed %s\n' "$$target"

check:
	$(PYTHON) -m compileall -q aegis pitcrew omni containers tests
	$(NODE) --check aegis/web/app.js
	$(NODE) --check aegis/web/workspace-tools.js
	$(NODE) --check aegis/web/session.js
	$(NODE) --check aegis/sample/app.js

test: check
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

coverage: check
	$(PYTHON) -m coverage erase
	$(PYTHON) -m coverage run -m unittest discover -s tests -p 'test_*.py' -v
	$(PYTHON) -m coverage combine -q
	$(PYTHON) -m coverage report

images: image-worker image-qa image-runtime

image-worker image-qa image-runtime: image-%:
	$(DOCKER) build --file containers/Dockerfile --target $* --tag aegis-$*:local .

run:
	$(PYTHON) -m aegis serve

doctor:
	$(PYTHON) -m aegis --doctor

smoke:
	$(PYTHON) tests/smoke.py
