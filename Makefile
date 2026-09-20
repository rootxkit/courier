# Courier — developer entry points.
#
# Every acceptance criterion in TASKS.md is phrased as a make target, so this
# file is the contract. Recipes are POSIX shell: on Windows run them from Git
# Bash or WSL.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

.PHONY: help hooks

help: ## Show this help
	@echo "Courier — available targets:"
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

hooks: ## Install the repository git hooks via core.hooksPath
	@git config core.hooksPath .githooks
	@chmod +x .githooks/* 2>/dev/null || true
	@echo "hooks: core.hooksPath -> .githooks"
