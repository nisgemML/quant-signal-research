PY      ?= python
SYMBOL  ?= BTCUSDT
START   ?= 2024-07
END     ?= 2025-06

.PHONY: install data test lint report notebook all clean

install:
	$(PY) -m pip install -e ".[dev]"

data:
	$(PY) -m qsr.data download --symbol $(SYMBOL) --start $(START) --end $(END) --dest data/raw

test:
	$(PY) -m pytest

lint:
	ruff check src tests scripts
	ruff format --check src tests scripts

report:
	$(PY) -m qsr.report --symbol $(SYMBOL) --start $(START) --end $(END) --update-readme

notebook:
	cd notebooks && QSR_START=$(START) QSR_END=$(END) $(PY) -m nbconvert --to notebook --execute --inplace 01_signal_study.ipynb

all: lint test data report notebook

clean:
	rm -rf data/synthetic .pytest_cache .ruff_cache
