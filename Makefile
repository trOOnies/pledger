install:
	python -m pip install -r requirements.txt
run:
	python src/main.py
lint:
	ruff check
