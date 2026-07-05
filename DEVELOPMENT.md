# Development

## Installation

Install the project in editable mode with the development dependencies:

```sh
python -m pip install -e ".[dev]"
```

Install the optional status-bar app dependencies too when you need to work on the tray UI:

```sh
python -m pip install -e ".[dev,gui]"
```

Install from wheel distributions built from the source tree including gui dependencies:

```sh
python -m pip install "marker_pdf_agent-*.whl[gui]"
```

## Tests

Run the deterministic test suite:

```sh
python -m pytest
```

Run the live Ollama routing check explicitly when Ollama and `llama3.1` are installed:

```sh
python -m pytest -m live_ollama
```

## Quality Checks

```sh
python -m black --check marker_pdf_agent tests
python -m ruff check marker_pdf_agent tests
python -m flake8 marker_pdf_agent tests
python -m mypy marker_pdf_agent tests
```

## Publish

Build and validate the source and wheel distributions before upload:

```sh
rm -rf dist
python -m build
python -m twine check dist/*
```

Upload to TestPyPI first:

```sh
python -m twine upload --repository testpypi dist/*
```

Upload to PyPI after verifying the TestPyPI package:

```sh
python -m twine upload dist/*
```

## Code Quality

Before opening a pull request or publishing a release, run the formatter, linters, type checker, tests, and package validation from the repository root:

```sh
python -m black marker_pdf_agent tests
python -m ruff check marker_pdf_agent tests --fix
python -m flake8 marker_pdf_agent tests
python -m mypy marker_pdf_agent tests
python -m pytest -m "not live_ollama"
rm -rf dist
python -m build
python -m twine check dist/*
```

Use check-only mode when you want to verify the tree without changing files:

```sh
python -m black --check marker_pdf_agent tests
python -m ruff check marker_pdf_agent tests
python -m flake8 marker_pdf_agent tests
python -m mypy marker_pdf_agent tests
python -m pytest -m "not live_ollama"
```

Run the live Ollama check separately because it depends on local model behavior:

```sh
python -m pytest -m live_ollama
```
