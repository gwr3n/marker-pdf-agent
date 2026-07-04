# marker-pdf-agent

A small Python background worker that watches the folder where it is launched, queues newly moved-in documents, converts them to Markdown with `marker-pdf`, and places the converted artifact into a routed subfolder.

## Install

Use the local virtual environment:

```sh
venv/bin/python -m pip install -r requirements.txt
```

`ollama` is optional. If the `ollama` CLI is present and a recent fast model such as `llama3.1`, `llama3`, `mistral`, `phi3`, or `gemma2` is installed, the worker uses it to choose the destination folder. Otherwise converted files go to `converted/uncategorized`.

## Run

From the folder you want the agent to manage:

```sh
venv/bin/python -m marker_pdf_agent.worker
```

By default the worker creates and uses these folders:

- `incoming/` - move documents here for conversion
- `.marker-pdf-agent/processing/` - temporary in-progress files
- `.marker-pdf-agent/failed/` - source files that failed conversion
- `converted/<category>/` - original documents plus final Markdown or zip artifacts

If conversion produces only Markdown, the final artifact is a `.md` file. If marker emits images or other assets, the final artifact is a `.zip` containing the Markdown plus assets. The original document is moved into the same category folder as the converted artifact.

## Options

```sh
venv/bin/python -m marker_pdf_agent.worker \
  --root /path/to/folder \
  --incoming incoming \
  --converted converted \
  --marker-command marker_single \
  --ollama-model llama3.1
```

Useful flags:

- `--root`: manage a folder other than the current working directory
- `--incoming`: choose the watched subfolder
- `--converted`: choose the output subfolder
- `--marker-command`: choose the `marker-pdf` executable, defaults to `marker_single`
- `--ollama-model`: force a specific installed Ollama model
- `--no-ollama`: disable AI folder routing

## Tests

```sh
venv/bin/python -m pytest
```

Run the live Ollama routing check explicitly when Ollama and `llama3.1` are installed:

```sh
venv/bin/python -m pytest -m live_ollama
```
