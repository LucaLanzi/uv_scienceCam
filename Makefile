# Makefile for the Arducam network stream
#
#   make setup              install everything (system packages, Python venv, camera permissions)
#   make run CAMERA=0       start streaming camera 0 on port 8080
#   make help               list all targets

PYTHON ?= python3
VENV   := .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python

# Override on the command line, e.g.  make run CAMERA=/dev/video2 PORT=9000 ARGS="--auth me:secret"
CAMERA ?= 0
PORT   ?= 8080
ARGS   ?=

# The real (non-root) user, even when make is run with sudo
TARGET_USER := $(or $(SUDO_USER),$(USER),$(shell id -un))

.PHONY: help setup system-deps venv video-group run test list formats clean

help:
	@echo "make setup        Install v4l-utils, create .venv with Python deps, add you to the 'video' group"
	@echo "make run          Stream the camera  (CAMERA=$(CAMERA) PORT=$(PORT) ARGS=...)"
	@echo "make list         List connected cameras"
	@echo "make formats      List resolutions/fps supported by CAMERA"
	@echo "make test         Run with a synthetic test pattern (no camera needed)"
	@echo "make clean        Remove .venv and caches"

setup: system-deps venv video-group
	@echo ""
	@echo "Setup complete. Start streaming with:  make run CAMERA=0"

# v4l-utils provides v4l2-ctl (used for the image controls).
# python3-venv is needed because recent Debian/Ubuntu/Raspberry Pi OS block
# system-wide pip installs, so the Python packages go into a local .venv.
system-deps:
	sudo apt-get update
	sudo apt-get install -y v4l-utils python3-venv python3-pip

venv: $(VENV)/.installed

$(VENV)/.installed: requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	touch $@

# Lets the user open /dev/video* without sudo. Takes effect after logging out and back in.
video-group:
	@if id -nG "$(TARGET_USER)" | grep -qw video; then \
		echo "$(TARGET_USER) is already in the 'video' group."; \
	else \
		sudo usermod -aG video "$(TARGET_USER)" && \
		echo "Added $(TARGET_USER) to the 'video' group. Log out and back in (or reboot) for it to take effect."; \
	fi

run: venv
	$(PY) arducam_stream.py --camera $(CAMERA) --port $(PORT) $(ARGS)

test: venv
	$(PY) arducam_stream.py --camera test --port $(PORT) $(ARGS)

list: venv
	$(PY) arducam_stream.py --list

formats: venv
	$(PY) arducam_stream.py --camera $(CAMERA) --list-formats

clean:
	rm -rf $(VENV) __pycache__
