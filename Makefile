install:
	python -m pip install -e ".[dev]"
build:
	python -m build
run:
	python main.py
lint:
	ruff check
