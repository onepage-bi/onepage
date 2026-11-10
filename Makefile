# OnePage developer entry point.
#
#   make help      list all targets (default)
#   make dev       start the app in Docker at http://localhost:8000
#   make demo      same, plus demo data
#   make run       start without Docker (needs `make venv` first)
#
# Requires GNU make (3.81 or newer) and bash. On Windows use WSL.
# Python for the local venv: 3.12 or 3.13 (3.14 breaks SQLAlchemy 2.0.36).
# Override with BOOT_PYTHON=/path/to/python3.12 make venv
# Override the interpreter for scripts with PY=/path/to/python make test

SHELL := /bin/bash
.DEFAULT_GOAL := help

DEV_URL := http://localhost:8000
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
BOOT_PYTHON ?=

# SAML is switched off for run and test. Dev SAML settings in .env (copied
# from .env.example) would make preflight refuse to start, and the copied
# OP_SAML_IDP_CERT value is a comment line, not an empty value. `make venv`
# does not install python3-saml either.
SAML_OFF = OP_SAML_IDP_ENTITY_ID= OP_SAML_IDP_SSO_URL= OP_SAML_IDP_CERT=

# Docker is checked before every docker call, so a missing or stopped daemon
# gets a plain explanation instead of the raw client error.
DOCKER_CHECK = \
	command -v docker >/dev/null 2>&1 || { echo "Docker is not installed. Install Docker Desktop (https://www.docker.com/products/docker-desktop/) and run this again."; exit 1; }; \
	docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is missing. Update Docker Desktop, or install the docker-compose-plugin package on Linux."; exit 1; }; \
	docker info >/dev/null 2>&1 || { echo "Docker is installed but not running. Start Docker Desktop (on Linux, start the docker service and check that your user is in the docker group), wait until it reports it is running, then run this again."; exit 1; }

# Poll /health for up to two minutes. Only a reply carrying OnePage's own
# "started_at" key counts, so another app answering on port 8000 cannot pass.
# Stops early if the container has exited, and prints the last log lines.
WAIT_HEALTH = \
	i=0; until curl -fsS --max-time 3 $(DEV_URL)/health 2>/dev/null | grep -q '"started_at"'; do \
	  if [ -z "$$(docker compose ps -q --status running onepage 2>/dev/null)" ] && [ $$i -gt 5 ]; then \
	    echo ""; echo "The OnePage container stopped. Last log lines:"; docker compose logs --tail 40 onepage; \
	    echo "Fix the error above, then run this again."; exit 1; \
	  fi; \
	  i=$$((i+1)); if [ $$i -gt 60 ]; then \
	    echo ""; echo "OnePage did not answer on $(DEV_URL)/health within two minutes. Another app may hold port 8000 (check: docker ps)."; \
	    echo "Last log lines:"; \
	    docker compose logs --tail 40 onepage; echo "Fix the error above, then run this again."; exit 1; \
	  fi; \
	  printf "."; sleep 2; \
	done; echo ""

.PHONY: help venv dev demo run assets test lint down clean

help: ## Show this list of targets
	@echo "OnePage make targets:"
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-8s %s\n", $$1, $$2}'
	@echo ""
	@echo "Start here: make dev   (needs Docker running)   or   make venv && make run"

venv: ## Create .venv and install Python requirements (no Docker needed)
	@if [ -x .venv/bin/python ]; then \
	  echo "Using existing .venv"; \
	else \
	  B="$(BOOT_PYTHON)"; [ -n "$$B" ] || B=$$(command -v python3.12 || command -v python3.13 || command -v python3 || true); \
	  if [ -z "$$B" ]; then echo "Python 3 was not found. Install Python 3.12 (https://www.python.org/downloads/) and run this again."; exit 1; fi; \
	  if ! "$$B" -c 'import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] <= (3, 13) else 1)'; then \
	    echo "Found $$B ($$("$$B" -V 2>&1)), but OnePage needs Python 3.12 or 3.13 (3.14 breaks SQLAlchemy 2.0.36)."; \
	    echo "Install Python 3.12, then run: BOOT_PYTHON=/path/to/python3.12 make venv"; exit 1; \
	  fi; \
	  echo "Creating .venv with $$B"; "$$B" -m venv .venv || { echo "Could not create .venv."; exit 1; }; \
	fi
	.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

dev: ## Start OnePage in Docker at http://localhost:8000 (first run builds the image)
	@$(DOCKER_CHECK)
	@if ! curl -fsS --max-time 2 $(DEV_URL)/health 2>/dev/null | grep -q '"started_at"'; then \
	  if (exec 3<>/dev/tcp/127.0.0.1/8000) 2>/dev/null; then \
	    echo "Port 8000 is already in use, so OnePage cannot start on $(DEV_URL)."; \
	    echo "Containers publishing it:"; docker ps --filter publish=8000 --format '  {{.Names}}'; \
	    echo "Stop that program (for example: docker stop <name>) and run make dev again."; exit 1; \
	  fi; \
	fi
	@mkdir -p data
	@if [ ! -f .env ]; then cp .env.example .env && \
	  echo "Created .env from .env.example (used by 'make run'; docker-compose.yml sets its own values)."; fi
	docker compose up --build -d
	@$(WAIT_HEALTH)
	@echo ""
	@echo "OnePage is running:  $(DEV_URL)"
	@echo "Sign in (dev):       $(DEV_URL)/dev-login"
	@echo "Demo data:           make demo"
	@echo "Stop:                make down"

demo: dev deps ## Start in Docker and load demo data (tools/seed_demo.py)
	@test -f tools/seed_demo.py || { echo "tools/seed_demo.py is missing; cannot seed demo data."; exit 1; }
	$(PY) tools/seed_demo.py --url $(DEV_URL)

# OP_INITIAL_ADMIN defaults to admin, as in docker-compose.yml. Without it the
# stand came up but there was NOBODY to sign in as: .env.example leaves the variable
# empty, app/main.py creates the administrator only when it is set, and
# preflight stays silent with dev-bypass on. The second quickstart path
# ended with the refusal "User 'admin' not found".
run: ## Start without Docker at http://localhost:8000 (run `make venv` first)
	@test -x .venv/bin/python || { echo "No .venv yet. Run: make venv"; exit 1; }
	@mkdir -p data
	@echo "Starting OnePage without Docker: $(DEV_URL)"
	@echo "Sign in (dev):  $(DEV_URL)/dev-login"
	env OP_HOST=127.0.0.1 OP_PORT=8000 OP_INITIAL_ADMIN=$${OP_INITIAL_ADMIN:-admin} OP_DEV_BYPASS_AUTH=true $(SAML_OFF) \
	  .venv/bin/python entrypoint.py

assets: ## Download browser engine files (pyodide, duckdb-wasm, ~105 MB; needs internet)
	@echo "Downloading ~105 MB of browser engine files from the internet."
	$(PY) deploy/fetch-pyodide.py
	$(PY) deploy/fetch-duckdb-wasm.py

test: ## Run smoke tests (python, node, i18n); lists failures, exits 1 if any fail
	@command -v node >/dev/null 2>&1 || { echo "Node.js is required for tests/*.js (https://nodejs.org)."; exit 1; }
	@if [ ! -x .venv/bin/python ]; then echo "No .venv found, using $(PY). If imports fail, run: make venv"; fi
	@log=$$(mktemp -d "$${TMPDIR:-/tmp}/onepage-test.XXXXXX"); \
	fails=""; n=0; nf=0; \
	for t in tests/smoke_*.py tools/i18n_check.py tests/*.js; do \
	  name=$$(basename "$$t"); \
	  case "$$name" in _*) continue;; esac; \
	  case "$$t" in *.py) runner="$(PY)";; *) runner=node;; esac; \
	  n=$$((n+1)); \
	  if env $(SAML_OFF) $$runner "$$t" >"$$log/$$name.log" 2>&1; then printf "."; \
	  else printf "F"; fails="$$fails $$t"; nf=$$((nf+1)); fi; \
	done; \
	echo ""; \
	if [ -n "$$fails" ]; then \
	  echo ""; echo "Failed tests:"; \
	  for t in $$fails; do \
	    name=$$(basename "$$t"); \
	    echo "  $$t"; \
	    why=$$(grep -E '^FAIL |Error' "$$log/$$name.log" | head -n 1); \
	    [ -n "$$why" ] || why=$$(grep -v '^[[:space:]]*$$' "$$log/$$name.log" | tail -n 1); \
	    echo "      $$(echo "$$why" | cut -c1-160)"; \
	    if [ "$$name" = "smoke_i18n.py" ]; then \
	      echo "      note: smoke_i18n.py depends on the day of the week and fails on Saturday and Sunday (pre-existing)."; \
	    fi; \
	    echo "      log: $$log/$$name.log"; \
	  done; \
	  echo ""; echo "$$((n-nf)) of $$n tests passed, $$nf failed."; \
	  exit 1; \
	fi; \
	rm -rf "$$log"; \
	echo "All $$n tests passed."

lint: ## Check i18n strings and JS syntax (node --check on static/js)
	@command -v node >/dev/null 2>&1 || { echo "Node.js is required for the JS syntax check (https://nodejs.org)."; exit 1; }
	$(PY) tools/i18n_check.py
# Two traps in a row, both produce "lint: clean" over broken JavaScript.
# First: `-exec ... {} \;` returns 0 regardless of node --check's exit code.
# Second, the one the fix itself broke on: `node --check` checks ONLY
# the first file and silently ignores the rest, so `-exec ... {} +` lies
# too. Hence an explicit loop, one file at a time, accumulating failures.
	@fail=0; \
	for f in $$(find static/js -name '*.js' -not -path '*/vendor/*' | sort); do \
	  node --check "$$f" || { echo "  broken: $$f"; fail=1; }; \
	done; \
	if [ $$fail -eq 0 ]; then echo "lint: clean"; \
	else echo "lint: FAILED"; exit 1; fi

down: ## Stop the Docker containers (keeps data/)
	@$(DOCKER_CHECK)
	docker compose down

clean: ## Stop Docker and delete data/ and .venv/ (asks first)
	@printf "This deletes data/ (your local database and uploads) and .venv/. Type yes to continue: "; \
	read ans; [ "$$ans" = "yes" ] || { echo "Aborted, nothing was deleted."; exit 1; }
	@if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then docker compose down -v; fi
	rm -rf data .venv
	@echo "Removed data/ and .venv/. Your .env was kept."

# ── Dependency guard ───────────────────────────────────────────────────────
# `make demo` runs tools/seed_demo.py on the HOST while the server runs in
# Docker, so the host interpreter needs httpx. Without this the newcomer saw
# "OnePage is running" and then a raw ModuleNotFoundError — the promise broken
# one step after it was made. Checking beats assuming: the system python3 may
# also be a version `make venv` itself refuses.
.PHONY: deps
deps: ## Make sure the interpreter running tools/ has what they import
	@$(PY) -c "import httpx" 2>/dev/null && exit 0 || true; \
	if [ -x .venv/bin/python ]; then \
	  echo "Installing host dependencies into .venv ..."; \
	  .venv/bin/pip install -q -r requirements.txt; \
	else \
	  echo "The tools need httpx and this interpreter ($(PY)) does not have it."; \
	  echo "Run 'make venv' once, then repeat this command."; \
	  exit 1; \
	fi
