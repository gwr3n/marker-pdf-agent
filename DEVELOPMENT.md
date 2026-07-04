# Development

## Tests

Run the deterministic test suite:

```sh
venv/bin/python -m pytest
```

Run the live Ollama routing check explicitly when Ollama and `llama3.1` are installed:

```sh
venv/bin/python -m pytest -m live_ollama
```

## Quality Checks

```sh
venv/bin/python -m black --check marker_pdf_agent tests
venv/bin/python -m ruff check marker_pdf_agent tests
venv/bin/python -m flake8 marker_pdf_agent tests
venv/bin/python -m mypy marker_pdf_agent tests
```

## Publish

Build and validate the source and wheel distributions before upload:

```sh
rm -rf dist
venv/bin/python -m build
venv/bin/python -m twine check dist/*
```

Upload to TestPyPI first:

```sh
venv/bin/python -m twine upload --repository testpypi dist/*
```

Upload to PyPI after verifying the TestPyPI package:

```sh
venv/bin/python -m twine upload dist/*
```
