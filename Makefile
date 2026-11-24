PYTHON ?= python3
NODE ?= node
DOCKER ?= docker

.PHONY: all build check test images image-worker image-qa image-runtime run doctor smoke

all: build images

build: check
	$(PYTHON) -m omni.build

check:
	$(PYTHON) -m compileall -q omni containers tests
	$(NODE) --check omni/web/app.js
	$(NODE) --check omni/web/session.js

test: check
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

images: image-worker image-qa image-runtime

image-worker image-qa image-runtime: image-%:
	$(DOCKER) build --file containers/Dockerfile --target $* --tag rittaiharness-$*:local .

run:
	$(PYTHON) -m omni

doctor:
	$(PYTHON) -m omni --doctor

smoke:
	$(PYTHON) tests/smoke.py
