# SME RM Copilot prototype. Everything runs locally against mocks.
PY := .venv/bin/python

.PHONY: setup demo test evals serve static clean

setup:            ## create .venv and install the package with dev tools
	python3 -m venv .venv
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -e ".[dev]"

demo:             ## run every scenario in the terminal, with traces grouped by lane
	$(PY) demo/run_demo.py

test:             ## unit, end-to-end, security and eval tests
	$(PY) -m pytest

evals:            ## release gate: golden set must pass before any release
	$(PY) -m copilot.operations.evals.release_gate

serve:            ## FastAPI app with the demo UI on http://127.0.0.1:8000
	COPILOT_DEV=1 $(PY) -m uvicorn copilot.app:app --host 127.0.0.1 --port 8000

static:           ## self-contained static demo page at demo/dist/index.html
	$(PY) demo/export_static.py

clean:
	rm -rf .venv .pytest_cache build *.egg-info src/*.egg-info
