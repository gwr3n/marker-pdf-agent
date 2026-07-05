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
