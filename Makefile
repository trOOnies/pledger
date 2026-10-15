.PHONY: help
default: help

help: ## Display this help screen
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
install: ## Install dependencies
	python -m pip install -e ".[dev]"
build: ## Build the library version
	python -m build
serve: ## Run the web app on http://127.0.0.1:8000
	python -m pledger.web --reload
test: ## Run the test suite
	python -m pytest --cov
lint: ## Apply the linter
	ruff check --fix
