# marker-pdf-agent

A small Python document-conversion agent for turning PDFs and other supported documents into Markdown with `marker-pdf`. It watches one or more managed folders, queues documents moved into each `incoming/` directory, processes them through a single conversion worker, and stores the original plus the converted Markdown or asset zip under `converted/<category>/`.

The agent can run as a plain foreground worker, an optional tray/menu-bar app, or a user-level background service. Folder routing is local and deterministic by default, with optional Ollama-assisted category selection when explicitly enabled.

## Install

Use the local virtual environment:

```sh
venv/bin/python -m pip install -r requirements.txt
```

Install the optional desktop GUI dependencies when you want the status-bar app:

```sh
venv/bin/python -m pip install ".[gui]"
```

When installing from a built wheel, put the extra on the wheel filename:

```sh
venv/bin/python -m pip install "dist/marker_pdf_agent-0.1.0-py3-none-any.whl[gui]"
```

`ollama` is optional and is not started or queried unless you explicitly pass `--ollama-model`. Without that flag, converted files go to `converted/uncategorized`.

## Run

From the folder you want the agent to manage:

```sh
venv/bin/python -m marker_pdf_agent.worker run
```

For compatibility, running without the `run` subcommand still starts the foreground worker.

## Status-Bar GUI

The status-bar GUI is for synchronous foreground runs, not installed daemon/service runs. It uses the same worker manager as the command-line foreground worker, shows a compact `Idle` or `Converting` state plus queue size, and still allows only one `marker-pdf` conversion at a time. On macOS it runs as a menu-bar app rather than showing a Dock icon.

Install the optional GUI extra before using this mode:

```sh
venv/bin/python -m pip install ".[gui]"
```

Launch it with either command:

```sh
venv/bin/python -m marker_pdf_agent.worker tray --root /path/to/folder
venv/bin/python -m marker_pdf_agent.worker run --tray --root /path/to/folder
```

Click the status-bar icon to open the menu. The menu refreshes when opened and shows the worker state, queue length, and monitored folders. It also has controls to open a folder's `incoming/` or `converted/` directory, add or remove monitored folders, and quit the foreground worker cleanly. Detailed progress and routing messages are printed to stdout.

Use the `Ollama routing` submenu to choose `Disabled` or one of the installed Ollama models. Choose `Refresh models` to query `ollama list` in the background; the app does not query Ollama just from opening the tray menu. The selected model is persisted with the tray config and applies to all monitored folders.

Monitored folders and the selected Ollama model are persisted in `~/.marker-pdf-agent/config.json` by default. Use `--config /path/to/config.json` to choose a different config file. The `--root` folder passed at launch is added to that file automatically, and folders or model settings changed from the GUI update the same file.

Multiple monitored folders share one conversion queue and one converter loop. Files from any monitored `incoming/` folder may be queued, but only one `marker-pdf` subprocess runs at a time. Removing a monitored folder stops future scans and drops pending queued jobs for that folder.

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
- `--config`: choose the persisted foreground GUI config file, defaults to `~/.marker-pdf-agent/config.json`
- `--incoming`: choose the watched subfolder
- `--converted`: choose the output subfolder
- `--marker-command`: choose the `marker-pdf` executable, defaults to `marker_single`
- `--marker-timeout`: maximum seconds to allow one conversion before moving it to failed, defaults to 1800
- `--ollama-model`: enable AI folder routing with a specific installed Ollama model
- `--no-ollama`: disable AI folder routing; this is the default unless `--ollama-model` is set

## Background Service

The worker itself stays plain Python. Because `marker-pdf` can be GPU-heavy, the agent uses a user-level singleton lock and is intended to run as one background worker per user. That one worker owns the conversion queue and processes one document at a time.

Internally, foreground runs go through a worker manager with a single shared conversion queue. This is important for multi-folder and status-bar UI support: multiple monitored folders may enqueue documents, but only one converter loop drains the queue, so only one `marker-pdf` subprocess should use the GPU at a time.

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

## License

This project is released under the MIT License. See [LICENSE](LICENSE).

The tray icon is derived from the MIT-licensed `file-markdown` SVG from SVG Repo: <https://www.svgrepo.com/svg/332064/file-markdown>.
