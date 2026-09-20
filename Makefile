# Courier — developer entry points.
#
# Every acceptance criterion in TASKS.md is phrased as a make target, so this
# file is the contract. Recipes are POSIX shell: on Windows run them from Git
# Bash or WSL.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# Do NOT add --project-directory here. Compose resolves relative bind-mount
# paths against the project directory, so overriding it to the repository root
# makes ./initdb in the compose file point at a path that does not exist —
# Docker then creates it empty and the init SQL silently never runs. Leaving it
# unset puts the project directory at infra/, where those paths are correct.
#
# The consequence is that .env is no longer picked up automatically, so it is
# passed explicitly when present. The compose file defaults every variable, so
# a fresh clone with no .env still comes up.
ENV_FILE := $(wildcard .env)
COMPOSE := docker compose -f infra/docker-compose.dev.yml $(if $(ENV_FILE),--env-file $(ENV_FILE))

N ?= 1

PYTHON ?= $(shell command -v python3 2>/dev/null || command -v python)
VENV := .venv
ifeq ($(OS),Windows_NT)
VENV_BIN := $(VENV)/Scripts
else
VENV_BIN := $(VENV)/bin
endif

.PHONY: help hooks up down stop ps logs reset psql psql-telemetry sim sim-stop \
        venv lint fmt typecheck test test-cov clean

help: ## Show this help
	@echo "Courier — available targets:"
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

hooks: ## Install the repository git hooks via core.hooksPath
	@git config core.hooksPath .githooks
	@chmod +x .githooks/* 2>/dev/null || true
	@echo "hooks: core.hooksPath -> .githooks"

up: ## Start the dev stack and block until every service is healthy
	$(COMPOSE) up -d --wait
	@$(MAKE) --no-print-directory ps

down: ## Stop the dev stack, keeping data volumes
	$(COMPOSE) down

stop: ## Stop containers without removing them
	$(COMPOSE) stop

ps: ## Show stack status and health
	@$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'

logs: ## Tail stack logs (make logs S=postgres for one service)
	$(COMPOSE) logs -f --tail=100 $(S)

reset: ## Destroy the stack AND its data volumes, then start clean
	$(COMPOSE) down -v
	$(MAKE) --no-print-directory up

psql: ## Open a psql shell on the relational database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-courier} -d $${POSTGRES_DB:-courier}

psql-telemetry: ## Open a psql shell on the telemetry database
	$(COMPOSE) exec timescale psql -U $${TIMESCALE_USER:-courier} -d $${TIMESCALE_DB:-courier_telemetry}

sim: ## Launch N SITL vehicles (make sim N=10)
	./sim/run_sitl.sh -n $(N)

sim-stop: ## Stop every SITL vehicle
	./sim/stop_sitl.sh

$(VENV_BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(VENV_BIN)/python -m pip install --quiet --upgrade pip
	$(VENV_BIN)/python -m pip install --quiet -e '.[dev]'

venv: $(VENV_BIN)/python ## Create .venv and install the workspace with dev extras

fmt: venv ## Format and apply safe lint fixes
	$(VENV_BIN)/ruff format .
	$(VENV_BIN)/ruff check --fix .

lint: venv typecheck ## Lint and typecheck everything
	$(VENV_BIN)/ruff check .
	$(VENV_BIN)/ruff format --check .

typecheck: venv ## mypy, strict on the safety-relevant modules
	$(VENV_BIN)/mypy

test: venv ## Run unit tests (SITL integration tests excluded)
	$(VENV_BIN)/pytest -m 'not sitl'

test-cov: venv ## Run unit tests with a coverage report
	$(VENV_BIN)/pytest -m 'not sitl' --cov --cov-report=term-missing

clean: ## Remove caches, build output and the virtualenv
	rm -rf $(VENV) .mypy_cache .ruff_cache .pytest_cache .coverage htmlcov \
	       coverage.xml build dist ./*.egg-info sim/out
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
