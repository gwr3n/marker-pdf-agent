# marker-pdf-agent

[![CI](https://github.com/gwr3n/marker-pdf-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/gwr3n/marker-pdf-agent/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/marker-pdf-agent.svg)](https://pypi.org/project/marker-pdf-agent/)
[![PyPI - Wheel](https://img.shields.io/pypi/wheel/marker_pdf_agent)](https://pypi.org/project/marker-pdf-agent/)
[![GitHub last commit](https://img.shields.io/github/last-commit/gwr3n/marker-pdf-agent)](https://github.com/gwr3n/marker-pdf-agent/commits/main)
[![Downloads](https://static.pepy.tech/badge/marker-pdf-agent)](https://pepy.tech/project/marker-pdf-agent)
[![Python](https://img.shields.io/pypi/pyversions/marker-pdf-agent.svg)](https://pypi.org/project/marker-pdf-agent/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Code style: Black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Lint: Ruff](https://img.shields.io/badge/lint-ruff-46a2f1.svg)](https://docs.astral.sh/ruff/)
[![Types: Mypy](https://img.shields.io/badge/types-mypy-blue.svg)](https://mypy-lang.org/)

A small document-conversion agent for turning PDFs and other supported documents into Markdown with `marker-pdf`. It watches a folder, converts files dropped into `incoming/`, and saves the original plus the converted Markdown or asset zip under `converted/<category>/`.

Use it as a foreground command, an optional status-bar app, or a user-level background service. Folder routing is local by default, with optional Ollama-assisted category selection when you explicitly enable it.

## Install

Install from PyPI:

```sh
pip install marker-pdf-agent
```

Install the optional desktop dependencies if you want the status-bar app:

```sh
pip install "marker-pdf-agent[gui]"
```

Python 3.10 or newer is required. The core `marker-pdf` dependency is installed automatically.

`ollama` is optional and is not started or queried unless you explicitly pass `--ollama-model`. Without that flag, converted files go to `converted/uncategorized`.

## Run

Start the agent from the folder you want it to manage:

```sh
marker-pdf-agent run
```

Or choose a folder explicitly:

```sh
marker-pdf-agent run --root /path/to/folder
```

The agent creates the folders it needs on startup. Move documents into `incoming/`, wait for conversion, then collect the output from `converted/uncategorized/` unless you enabled Ollama routing.

## Status-Bar App

The status-bar app is for foreground use. It shows the current state, queue size, and monitored folders, and it can open the `incoming/` and `converted/` folders for you. On macOS it runs as a menu-bar app rather than showing a Dock icon.

Launch it with either command:

```sh
marker-pdf-agent tray
marker-pdf-agent tray --root /path/to/folder
```

Click the status-bar icon to open the menu. From there you can add or remove monitored folders, choose an Ollama routing model, refresh the installed Ollama model list, and quit cleanly. Detailed progress and routing messages are printed to the terminal that launched the app.

Use the `Ollama routing` submenu to choose `Disabled` or one of the installed Ollama models. Choose `Refresh models` to query `ollama list` in the background; the app does not query Ollama just from opening the tray menu. The selected model is persisted with the tray config and applies to all monitored folders.

Monitored folders and the selected Ollama model are persisted in `~/.marker-pdf-agent/config.json` by default. Use `--config /path/to/config.json` to choose a different config file. The `--root` folder passed at launch is added to that file automatically, and folders or model settings changed from the status-bar app update the same file.

Multiple monitored folders share one queue, so only one conversion runs at a time. Removing a monitored folder stops future scans and drops pending queued jobs for that folder.

By default the worker creates and uses these folders:

- `incoming/` - move documents here for conversion
- `.marker-pdf-agent/processing/` - temporary in-progress files
- `.marker-pdf-agent/failed/` - source files that failed conversion
- `converted/<category>/` - original documents plus final Markdown or zip artifacts

If conversion produces only Markdown, the final artifact is a `.md` file. If marker emits images or other assets, the final artifact is a `.zip` containing the Markdown plus assets. The original document is moved into the same category folder as the converted artifact.

If a conversion fails, times out, or is interrupted during shutdown, the source document is moved to `.marker-pdf-agent/failed/`. If the worker starts and finds leftover files in `.marker-pdf-agent/processing/` from a previous interrupted run, it moves them to `.marker-pdf-agent/failed/` so they are visible for manual retry.

## Options

```sh
marker-pdf-agent run \
  --root /path/to/folder \
  --incoming incoming \
  --converted converted \
  --marker-command marker_single \
  --marker-timeout 1800 \
  --ollama-model llama3.1
```

Useful flags:

- `--root`: manage a folder other than the current working directory
- `--config`: choose the persisted status-bar app config file, defaults to `~/.marker-pdf-agent/config.json`
- `--incoming`: choose the watched subfolder
- `--converted`: choose the output subfolder
- `--marker-command`: choose the `marker-pdf` executable, defaults to `marker_single`
- `--marker-timeout`: maximum seconds to allow one conversion before moving it to failed, defaults to 1800
- `--ollama-model`: enable AI folder routing with a specific installed Ollama model
- `--no-ollama`: disable AI folder routing; this is the default unless `--ollama-model` is set

## Background Service

Install a user-level background service when you want the agent to keep watching a folder after you close the terminal. Because `marker-pdf` can be GPU-heavy, the agent is designed to run as one worker per user and process one document at a time.

The installer detects the current operating system and writes the native service definition for that platform:

- macOS: user LaunchAgent plist in `~/Library/LaunchAgents/`
- Linux: systemd user unit in `~/.config/systemd/user/`
- Windows: setup instructions for NSSM or a pywin32 service wrapper in `.marker-pdf-agent/windows-service.md`

Install a service for a managed folder:

```sh
marker-pdf-agent install-service --root /path/to/folder
```

Then start it with the command printed by the installer. On macOS this is a `launchctl bootstrap ...` command. On Linux this is a `systemctl --user daemon-reload && systemctl --user enable --now ...` command. Windows needs an additional service host, because Python cannot install a native Windows service without one.

Check or remove the service definition:

```sh
marker-pdf-agent status
marker-pdf-agent uninstall-service
```

The `--service-name` option changes the installed service definition name. It is mainly useful when replacing or testing service definitions. Running multiple agent services at once is not recommended.

```sh
marker-pdf-agent install-service \
  --service-name marker-pdf-agent \
  --root /path/to/folder
```

Service logs are written under the managed folder in `.marker-pdf-agent/service.log` and `.marker-pdf-agent/service.err.log`.

## Development

Development, test, quality-check, and publishing notes are in [DEVELOPMENT.md](DEVELOPMENT.md).

## License

This project is released under the MIT License. See [LICENSE](LICENSE).

The tray icon is derived from the MIT-licensed `file-markdown` SVG from SVG Repo: <https://www.svgrepo.com/svg/332064/file-markdown>.
