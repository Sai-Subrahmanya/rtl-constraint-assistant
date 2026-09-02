# RTL Constraint Assistant — convenience commands
PY ?= python3
PIP ?= pip3

.PHONY: all install dev-install test lint clean example example-pipeline example-multiclock

all: install

install:
	$(PIP) install -e .

dev-install:
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest tests/ -v

test-cov:
	$(PY) -m pytest tests/ --cov=rca --cov-report=term-missing

lint:
	$(PY) -m ruff check src/ tests/

typecheck:
	$(PY) -m mypy src/rca --ignore-missing-imports

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf examples/*/output/ output/
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete

example:
	cd examples/simple_counter && rca report project.yaml

example-pipeline:
	cd examples/pipeline && rca report project.yaml

example-multiclock:
	cd examples/multi_clock && rca report project.yaml

dashboard:
	rca dashboard examples/simple_counter/project.yaml
