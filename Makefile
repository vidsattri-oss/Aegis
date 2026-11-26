PYTHON ?= python3
NODE ?= node
DOCKER ?= docker
PREFIX ?= $(HOME)/.local

.PHONY: all build install check test images image-worker image-qa image-runtime run doctor smoke

all: build images

build: check
	$(PYTHON) -m pitcrew.build

install: build
	install -d "$(DESTDIR)$(PREFIX)/bin"
	@set -eu; \
	  target="$(DESTDIR)$(PREFIX)/bin/pitcrew"; \
	  test ! -d "$$target"; \
	  tmp=$$(mktemp "$$target.XXXXXX"); \
	  trap 'rm -f "$$tmp"' EXIT; \
	  install -m 0755 dist/pitcrew "$$tmp"; \
	  mv -f "$$tmp" "$$target"; \
	  printf 'Installed %s\n' "$$target"

check:
	$(PYTHON) -m compileall -q pitcrew omni containers tests
	$(NODE) --check pitcrew/web/app.js
	$(NODE) --check pitcrew/web/workspace-tools.js
	$(NODE) --check pitcrew/web/session.js
	$(NODE) --check pitcrew/sample/app.js

test: check
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

images: image-worker image-qa image-runtime

image-worker image-qa image-runtime: image-%:
	$(DOCKER) build --file containers/Dockerfile --target $* --tag pitcrew-$*:local .

run:
	$(PYTHON) -m pitcrew serve

doctor:
	$(PYTHON) -m pitcrew --doctor

smoke:
	$(PYTHON) tests/smoke.py
