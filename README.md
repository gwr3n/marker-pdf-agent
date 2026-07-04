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
venv/bin/python -m marker_pdf_agent.worker run
```

For compatibility, running without the `run` subcommand still starts the foreground worker.

By default the worker creates and uses these folders:

- `incoming/` - move documents here for conversion
- `.marker-pdf-agent/processing/` - temporary in-progress files
- `.marker-pdf-agent/failed/` - source files that failed conversion
- `converted/<category>/` - original documents plus final Markdown or zip artifacts

If conversion produces only Markdown, the final artifact is a `.md` file. If marker emits images or other assets, the final artifact is a `.zip` containing the Markdown plus assets. The original document is moved into the same category folder as the converted artifact.

If a conversion fails, times out, or is interrupted during shutdown, the source document is moved to `.marker-pdf-agent/failed/`. If the worker starts and finds leftover files in `.marker-pdf-agent/processing/` from a previous interrupted run, it moves them to `.marker-pdf-agent/failed/` so they are visible for manual retry.

## Options

```sh
venv/bin/python -m marker_pdf_agent.worker \
  --root /path/to/folder \
  --incoming incoming \
  --converted converted \
  --marker-command marker_single \
  --marker-timeout 1800 \
  --ollama-model llama3.1
```

Useful flags:

- `--root`: manage a folder other than the current working directory
- `--incoming`: choose the watched subfolder
- `--converted`: choose the output subfolder
- `--marker-command`: choose the `marker-pdf` executable, defaults to `marker_single`
- `--marker-timeout`: maximum seconds to allow one conversion before moving it to failed, defaults to 1800
- `--ollama-model`: force a specific installed Ollama model
- `--no-ollama`: disable AI folder routing

## Background Service

The worker itself stays plain Python. Because `marker-pdf` can be GPU-heavy, the agent uses a user-level singleton lock and is intended to run as one background worker per user. That one worker owns the conversion queue and processes one document at a time.

The service CLI detects the current operating system and writes the native service definition for that platform:

- macOS: user LaunchAgent plist in `~/Library/LaunchAgents/`
- Linux: systemd user unit in `~/.config/systemd/user/`
- Windows: setup instructions for NSSM or a pywin32 service wrapper in `.marker-pdf-agent/windows-service.md`

Install a service for a managed folder:

```sh
venv/bin/python -m marker_pdf_agent.worker install-service --root /path/to/folder
```

Then start it with the command printed by the installer. On macOS this is a `launchctl bootstrap ...` command. On Linux this is a `systemctl --user daemon-reload && systemctl --user enable --now ...` command. Windows needs an additional service host, because Python cannot install a native Windows service without one.

Check or remove the service definition:

```sh
venv/bin/python -m marker_pdf_agent.worker status
venv/bin/python -m marker_pdf_agent.worker uninstall-service
```

The `--service-name` option changes the installed service definition name. It is mainly useful when replacing or testing service definitions; running multiple agent services at once is not recommended, and the runtime lock prevents concurrent worker processes for the same user.

```sh
venv/bin/python -m marker_pdf_agent.worker install-service \
  --service-name marker-pdf-agent \
  --root /path/to/folder
```

Service logs are written under the managed folder in `.marker-pdf-agent/service.log` and `.marker-pdf-agent/service.err.log`.

## Tests

```sh
venv/bin/python -m pytest
```

Run the live Ollama routing check explicitly when Ollama and `llama3.1` are installed:

```sh
venv/bin/python -m pytest -m live_ollama
```
