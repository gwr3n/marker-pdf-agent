from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
    ".docx",
    ".pptx",
    ".xlsx",
    ".html",
    ".epub",
}
SYSTEM_FOLDERS = {"incoming", "processing", "converted", "failed", ".marker-pdf-agent"}
ROUTING_RESERVED_FOLDERS = SYSTEM_FOLDERS | {"uncategorized"}
DEFAULT_SERVICE_NAME = "marker-pdf-agent"
DEFAULT_CONFIG_NAME = "config.json"
GUI_DEPENDENCY_ERROR = (
    "The status-bar app requires optional GUI dependencies. "
    'Install them with: python -m pip install "marker-pdf-agent[gui]"'
)


@dataclass(frozen=True)
class WorkerConfig:
    root: Path
    incoming_dir: Path
    processing_dir: Path
    converted_dir: Path
    failed_dir: Path
    poll_interval: float
    stable_seconds: float
    marker_command: str
    marker_timeout: float
    ollama_model: str | None
    use_ollama: bool


@dataclass(frozen=True)
class ConversionJob:
    source: Path
    root: Path


@dataclass(frozen=True)
class WorkerStatus:
    roots: tuple[Path, ...]
    queue_size: int
    current_document: str | None
    current_root: Path | None
    stopping: bool
    last_message: str | None


class MarkerPdfWorker:
    def __init__(
        self,
        config: WorkerConfig,
        documents: queue.Queue[ConversionJob] | None = None,
        stop_event: threading.Event | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.documents: queue.Queue[ConversionJob] = documents or queue.Queue()
        self.stop_event = stop_event or threading.Event()
        self.on_progress = on_progress
        self.seen: set[Path] = set()

    def run(self) -> None:
        WorkerManager([self.config]).run()

    def _ensure_dirs(self) -> None:
        for directory in (
            self.config.incoming_dir,
            self.config.processing_dir,
            self.config.converted_dir,
            self.config.failed_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.config.processing_dir.iterdir()):
            if path.is_file():
                self._move_to_failed(path)

    def _scan_once(self) -> None:
        for path in sorted(self.config.incoming_dir.iterdir()):
            if not path.is_file() or path in self.seen or path.suffix.lower() not in DOCUMENT_EXTENSIONS:
                continue
            if self._is_stable(path):
                self.seen.add(path)
                self.documents.put(ConversionJob(path, self.config.root))
                self._progress(f"Queued {path.name}")

    def _is_stable(self, path: Path) -> bool:
        try:
            first_size = path.stat().st_size
            first_mtime = path.stat().st_mtime
            time.sleep(self.config.stable_seconds)
            second = path.stat()
        except FileNotFoundError:
            return False
        return first_size == second.st_size and first_mtime == second.st_mtime

    def _process_document(self, source: Path) -> None:
        self._progress(f"Processing {source.name}")
        processing_source = self._move_to_processing(source)
        try:
            with tempfile.TemporaryDirectory(prefix="marker-output-") as temp_name:
                output_dir = Path(temp_name)
                self._progress(f"Converting {source.name} with {self.config.marker_command}")
                self._run_marker(processing_source, output_dir)
                markdown_files = sorted(output_dir.rglob("*.md"))
                if not markdown_files:
                    raise RuntimeError("marker-pdf did not produce a markdown file")

                primary_markdown = markdown_files[0]
                self._progress(f"Routing {source.name}")
                destination_folder = self._choose_destination(primary_markdown)
                destination_folder.mkdir(parents=True, exist_ok=True)
                self._progress(f"Packaging {source.name}")
                artifact = self._pack_or_select_artifact(output_dir, primary_markdown, processing_source.stem)
                destination = unique_path(destination_folder / artifact.name)
                shutil.move(str(artifact), destination)
                original_destination = unique_path(destination_folder / processing_source.name)
                shutil.move(str(processing_source), original_destination)
                self._progress(f"Converted {source.name} -> {destination.relative_to(self.config.root)}")
        except Exception:
            self._move_to_failed(processing_source)
            raise

    def _progress(self, message: str) -> None:
        print(message, flush=True)
        if self.on_progress:
            self.on_progress(message)

    def _move_to_processing(self, source: Path) -> Path:
        destination = unique_path(self.config.processing_dir / source.name)
        return Path(shutil.move(str(source), destination))

    def _move_to_failed(self, source: Path) -> None:
        if not source.exists():
            return
        destination = unique_path(self.config.failed_dir / source.name)
        shutil.move(str(source), destination)

    def _run_marker(self, source: Path, output_dir: Path) -> None:
        command = [self.config.marker_command, str(source), "--output_dir", str(output_dir)]
        try:
            process = subprocess.Popen(command)
        except FileNotFoundError as exc:
            raise RuntimeError(f"marker command not found: {self.config.marker_command}") from exc

        deadline = time.monotonic() + self.config.marker_timeout
        while process.poll() is None:
            if self.stop_event.is_set():
                terminate_process(process)
                raise RuntimeError("marker-pdf interrupted by shutdown")
            if time.monotonic() >= deadline:
                terminate_process(process)
                raise RuntimeError(f"marker-pdf timed out after {self.config.marker_timeout:g} seconds")
            time.sleep(0.5)

        if process.returncode != 0:
            raise RuntimeError(f"marker-pdf failed with exit code {process.returncode}")

    def _choose_destination(self, markdown_file: Path) -> Path:
        if not self.config.use_ollama or not self.config.ollama_model:
            return self.config.converted_dir / "uncategorized"

        existing_folders = [
            path.name
            for path in self.config.converted_dir.iterdir()
            if path.is_dir() and path.name not in SYSTEM_FOLDERS
        ]
        markdown_text = markdown_file.read_text(encoding="utf-8", errors="replace")[:6000]
        folder_name, routing_error = ask_ollama_for_folder(self.config.ollama_model, existing_folders, markdown_text)
        if routing_error:
            self._progress(routing_error)
        if not folder_name:
            return self.config.converted_dir / "uncategorized"
        destination_name = sanitize_folder_name(folder_name)
        if destination_name in ROUTING_RESERVED_FOLDERS:
            return self.config.converted_dir / "uncategorized"
        return self.config.converted_dir / destination_name

    def _pack_or_select_artifact(self, output_dir: Path, markdown_file: Path, stem: str) -> Path:
        files = [path for path in output_dir.rglob("*") if path.is_file()]
        if len(files) == 1 and files[0] == markdown_file:
            markdown_target = output_dir / f"{stem}.md"
            markdown_file.rename(markdown_target)
            return markdown_target

        zip_path = output_dir / f"{stem}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in files:
                archive.write(file_path, file_path.relative_to(output_dir))
        return zip_path


def list_ollama_models() -> list[str]:
    if shutil.which("ollama") is None:
        return []
    try:
        result = subprocess.run(["ollama", "list"], check=True, capture_output=True, text=True, timeout=15)
    except subprocess.SubprocessError:
        return []

    models: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            models.append(parts[0])
    return models


def ask_ollama_for_folder(
    model: str, existing_folders: Iterable[str], markdown_text: str
) -> tuple[str | None, str | None]:
    existing_folder_names = sorted(existing_folders)
    folder_list = ", ".join(existing_folder_names) or "none"
    prompt = (
        "You route converted documents into concise folder names. "
        "Choose the best destination based on both the converted document content and the current subfolder structure. "
        "Use an existing converted/ subfolder when it fits, "
        "or propose one new folder that fits naturally beside the current subfolders. "
        "Return only the folder name, no punctuation or explanation.\n\n"
        f"Current converted/ subfolders: {folder_list}\n\n"
        f"Document markdown excerpt:\n{markdown_text}"
    )
    try:
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return None, f"Ollama routing timed out for model {model}"
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if exc.stderr else f"exit code {exc.returncode}"
        return None, f"Ollama routing failed for model {model}: {detail}"
    except subprocess.SubprocessError as exc:
        return None, f"Ollama routing failed for model {model}: {exc}"

    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not first_line:
        return None, f"Ollama routing returned no folder for model {model}"
    return choose_folder_from_ollama_response(first_line, existing_folder_names), None


def choose_folder_from_ollama_response(response: str, existing_folders: Iterable[str]) -> str | None:
    existing = {sanitize_folder_name(folder) for folder in existing_folders}
    candidates = [sanitize_folder_name(part) for part in re.split(r"[/\\,;>|]+", response)]
    candidates = [candidate for candidate in candidates if candidate and candidate not in ROUTING_RESERVED_FOLDERS]
    for candidate in candidates:
        if candidate in existing:
            return candidate
    return candidates[0] if candidates else None


def sanitize_folder_name(value: str) -> str:
    cleaned = re.sub(r"[/\\]+", " ", value)
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", " ", cleaned).strip().strip(".")
    cleaned = re.sub(r"\s+", "-", cleaned).lower()
    return cleaned[:80] or "uncategorized"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a unique name for {path}")


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def service_label(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip(".-")
    return cleaned if "." in cleaned else f"local.{cleaned or 'marker-pdf-agent'}"


def service_state_dir(root: Path) -> Path:
    return root / ".marker-pdf-agent"


def service_run_arguments(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "marker_pdf_agent.worker",
        "run",
        "--root",
        str(Path(args.root).resolve()),
    ]
    command.extend(["--incoming", args.incoming])
    command.extend(["--converted", args.converted])
    command.extend(["--poll-interval", str(args.poll_interval)])
    command.extend(["--stable-seconds", str(args.stable_seconds)])
    command.extend(["--marker-command", args.marker_command])
    command.extend(["--marker-timeout", str(args.marker_timeout)])
    if args.ollama_model:
        command.extend(["--ollama-model", args.ollama_model])
    if args.no_ollama:
        command.append("--no-ollama")
    return command


def launcher_run_arguments(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "marker_pdf_agent.worker",
        "tray",
        "--root",
        str(Path(args.root).resolve()),
    ]
    if args.config:
        command.extend(["--config", str(Path(args.config).resolve())])
    command.extend(["--incoming", args.incoming])
    command.extend(["--converted", args.converted])
    command.extend(["--poll-interval", str(args.poll_interval)])
    command.extend(["--stable-seconds", str(args.stable_seconds)])
    command.extend(["--marker-command", args.marker_command])
    command.extend(["--marker-timeout", str(args.marker_timeout)])
    if args.ollama_model:
        command.extend(["--ollama-model", args.ollama_model])
    if args.no_ollama:
        command.append("--no-ollama")
    return command


def install_service(args: argparse.Namespace) -> None:
    system = platform.system()
    if system == "Darwin":
        path = install_launchd_service(args)
        print(f"Installed macOS LaunchAgent at {path}", flush=True)
        print(f"Start it with: launchctl bootstrap gui/$(id -u) {path}", flush=True)
        return
    if system == "Linux":
        path = install_systemd_user_service(args)
        print(f"Installed systemd user service at {path}", flush=True)
        start_command = f"systemctl --user daemon-reload && systemctl --user enable --now {args.service_name}.service"
        print(
            f"Start it with: {start_command}",
            flush=True,
        )
        return
    if system == "Windows":
        path = install_windows_service_instructions(args)
        print(f"Wrote Windows service setup instructions to {path}", flush=True)
        return
    raise RuntimeError(f"unsupported platform for service install: {system}")


def install_launcher(args: argparse.Namespace) -> None:
    system = platform.system()
    if system == "Darwin":
        path = install_macos_app_launcher(args)
    elif system == "Linux":
        path = install_linux_desktop_launcher(args)
    elif system == "Windows":
        path = install_windows_cmd_launcher(args)
    else:
        raise RuntimeError(f"unsupported platform for launcher install: {system}")
    print(f"Installed launcher at {path}", flush=True)


def uninstall_service(args: argparse.Namespace) -> None:
    system = platform.system()
    if system == "Darwin":
        path = launchd_plist_path(args.service_name)
        if path.exists():
            path.unlink()
        print(f"Removed macOS LaunchAgent definition at {path}", flush=True)
        print(f"Unload a running service with: launchctl bootout gui/$(id -u) {path}", flush=True)
        return
    if system == "Linux":
        path = systemd_user_service_path(args.service_name)
        if path.exists():
            path.unlink()
        print(f"Removed systemd user service definition at {path}", flush=True)
        print("Reload systemd with: systemctl --user daemon-reload", flush=True)
        return
    if system == "Windows":
        path = windows_service_instruction_path(Path(args.root).resolve())
        if path.exists():
            path.unlink()
        print(f"Removed Windows service setup instructions at {path}", flush=True)
        return
    raise RuntimeError(f"unsupported platform for service uninstall: {system}")


def service_status(args: argparse.Namespace) -> None:
    system = platform.system()
    if system == "Darwin":
        label = service_label(args.service_name)
        subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], check=False)
        return
    if system == "Linux":
        subprocess.run(["systemctl", "--user", "status", f"{args.service_name}.service"], check=False)
        return
    if system == "Windows":
        print(
            "Check the service manager you used to install the Windows service, such as NSSM or Services.msc.",
            flush=True,
        )
        return
    raise RuntimeError(f"unsupported platform for service status: {system}")


def install_launchd_service(args: argparse.Namespace) -> Path:
    root = Path(args.root).resolve()
    state_dir = service_state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    label = service_label(args.service_name)
    path = launchd_plist_path(args.service_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": label,
        "ProgramArguments": service_run_arguments(args),
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(root),
        "StandardOutPath": str(state_dir / "service.log"),
        "StandardErrorPath": str(state_dir / "service.err.log"),
    }
    with path.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=False)
    return path


def install_systemd_user_service(args: argparse.Namespace) -> Path:
    root = Path(args.root).resolve()
    state_dir = service_state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = systemd_user_service_path(args.service_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    command = " ".join(quote_systemd_argument(part) for part in service_run_arguments(args))
    content = "\n".join(
        [
            "[Unit]",
            "Description=marker-pdf-agent document conversion worker",
            "After=default.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={root}",
            f"ExecStart={command}",
            "Restart=on-failure",
            "RestartSec=5",
            f"StandardOutput=append:{state_dir / 'service.log'}",
            f"StandardError=append:{state_dir / 'service.err.log'}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
    return path


def install_windows_service_instructions(args: argparse.Namespace) -> Path:
    root = Path(args.root).resolve()
    state_dir = service_state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    command = service_run_arguments(args)
    path = windows_service_instruction_path(root)
    path.write_text(
        "\n".join(
            [
                "# marker-pdf-agent Windows service",
                "",
                "Python cannot install a native Windows service without an additional service host.",
                "Use NSSM or a pywin32 service wrapper with these values:",
                "",
                f"Service name: {args.service_name}",
                f"Application: {command[0]}",
                f"Arguments: {' '.join(command[1:])}",
                f"Startup directory: {root}",
                f"Stdout log: {state_dir / 'service.log'}",
                f"Stderr log: {state_dir / 'service.err.log'}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def install_macos_app_launcher(args: argparse.Namespace) -> Path:
    root = Path(args.root).resolve()
    app_path = root / f"{launcher_name(args.launcher_name)}.app"
    macos_dir = app_path / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)
    state_dir = service_state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "launcher.log"
    executable = macos_dir / "marker-pdf-agent"
    command = " ".join(shlex.quote(part) for part in launcher_run_arguments(args))
    preflight_lines = macos_launcher_preflight_lines(Path(sys.executable), log_path)
    executable.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                f"cd {shlex.quote(str(root))}",
                *preflight_lines,
                f"/usr/bin/nohup {command} >> {shlex.quote(str(log_path))} 2>&1 &",
                "exit 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    plist = {
        "CFBundleName": args.launcher_name,
        "CFBundleDisplayName": args.launcher_name,
        "CFBundleIdentifier": macos_launcher_bundle_identifier(app_path),
        "CFBundleExecutable": "marker-pdf-agent",
        "CFBundlePackageType": "APPL",
    }
    with (app_path / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=False)
    return app_path


def macos_launcher_bundle_identifier(app_path: Path) -> str:
    digest = hashlib.sha256(str(app_path).encode("utf-8")).hexdigest()[:12]
    return f"local.marker-pdf-agent.launcher.{digest}"


def macos_launcher_preflight_lines(python_path: Path, log_path: Path) -> list[str]:
    pyvenv_config = python_path.parent.parent / "pyvenv.cfg"
    if not pyvenv_config.exists():
        return []
    message = (
        f"Marker PDF Agent cannot read {pyvenv_config}. macOS may be blocking app access to this folder. "
        "Move the managed folder or virtual environment outside protected folders such as Downloads, Desktop, "
        "or Documents, or grant the launcher Full Disk Access, then reinstall the launcher."
    )
    alert = f"display alert {json.dumps('Marker PDF Agent')} message {json.dumps(message)} as critical"
    return [
        f"if ! /bin/cat {shlex.quote(str(pyvenv_config))} >/dev/null 2>&1; then",
        f"  /usr/bin/osascript -e {shlex.quote(alert)} >/dev/null 2>&1",
        f"  echo {shlex.quote(message)} >> {shlex.quote(str(log_path))}",
        "  exit 1",
        "fi",
    ]


def install_linux_desktop_launcher(args: argparse.Namespace) -> Path:
    root = Path(args.root).resolve()
    name = launcher_name(args.launcher_name)
    path = root / f"{name}.desktop"
    command = " ".join(quote_desktop_argument(part) for part in launcher_run_arguments(args))
    content = "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            f"Name={args.launcher_name}",
            "Comment=Launch the marker-pdf-agent status-bar app",
            f"Exec={command}",
            "Terminal=false",
            "Categories=Utility;",
            "",
        ]
    )
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def install_windows_cmd_launcher(args: argparse.Namespace) -> Path:
    root = Path(args.root).resolve()
    path = root / f"{launcher_name(args.launcher_name)}.cmd"
    command = subprocess.list2cmdline(launcher_run_arguments(args))
    path.write_text(f'@echo off\r\nstart "" {command}\r\n', encoding="utf-8")
    return path


def launchd_plist_path(name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{service_label(name)}.plist"


def systemd_user_service_path(name: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"


def windows_service_instruction_path(root: Path) -> Path:
    return service_state_dir(root) / "windows-service.md"


def launcher_name(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "-", name).strip(" .-")
    return cleaned or "marker-pdf-agent"


def quote_desktop_argument(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if not escaped or re.search(r"\s", escaped):
        return f'"{escaped}"'
    return escaped


def quote_systemd_argument(value: str) -> str:
    if not value or re.search(r"\s|[\\'\"]", value):
        return "'" + value.replace("'", "'\\''") + "'"
    return value


def singleton_lock_path() -> Path:
    return Path.home() / ".marker-pdf-agent" / "agent.lock"


class SingletonLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: object | None = None

    def __enter__(self) -> SingletonLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if not handle.read(1):
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(f"marker-pdf-agent is already running for this user; lock held at {self.path}") from exc

        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self.handle = handle

    def release(self) -> None:
        if self.handle is None:
            return
        handle = self.handle
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)  # type: ignore[attr-defined]
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            handle.close()  # type: ignore[attr-defined]
            self.handle = None


class WorkerManager:
    def __init__(self, configs: Sequence[WorkerConfig]) -> None:
        if not configs:
            raise ValueError("at least one worker config is required")
        self.documents: queue.Queue[ConversionJob] = queue.Queue()
        self.stop_event = threading.Event()
        self.current_job: ConversionJob | None = None
        self.last_message: str | None = None
        self._workers_lock = threading.Lock()
        self._status_listeners: list[Callable[[WorkerStatus], None]] = []
        self.workers = [self._make_worker(config) for config in configs]

    def _make_worker(self, config: WorkerConfig) -> MarkerPdfWorker:
        return MarkerPdfWorker(config, self.documents, self.stop_event, self._record_progress)

    def _record_progress(self, message: str) -> None:
        self.last_message = message
        self._notify_status()

    def run(self) -> None:
        for worker in self.worker_snapshot():
            worker._ensure_dirs()
        converter = threading.Thread(target=self._convert_loop, name="marker-converter")
        converter.start()
        watched = ", ".join(str(worker.config.incoming_dir) for worker in self.worker_snapshot())
        print(f"Watching {watched} for documents. Press Ctrl+C to stop.", flush=True)

        try:
            while not self.stop_event.is_set():
                workers = self.worker_snapshot()
                for worker in workers:
                    worker._scan_once()
                time.sleep(min(worker.config.poll_interval for worker in workers))
        finally:
            self.stop_event.set()
            converter.join()

    def _convert_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.documents.get(timeout=0.5)
            except queue.Empty:
                continue

            worker = self._worker_for_root(job.root)
            self.current_job = job
            self._notify_status()
            try:
                if worker is None:
                    print(f"Skipped {job.source.name}: monitored folder was removed", flush=True)
                    continue
                worker._process_document(job.source)
            except Exception as exc:  # noqa: BLE001 - keep manager alive after per-file failures.
                print(f"Failed to process {job.source.name}: {exc}", flush=True)
                if worker is not None:
                    worker._move_to_failed(job.source)
            finally:
                if worker is not None:
                    worker.seen.discard(job.source)
                self.current_job = None
                self.documents.task_done()
                self._notify_status()

    def _worker_for_root(self, root: Path) -> MarkerPdfWorker | None:
        for worker in self.worker_snapshot():
            if worker.config.root == root:
                return worker
        return None

    def worker_snapshot(self) -> list[MarkerPdfWorker]:
        with self._workers_lock:
            return list(self.workers)

    def add_config(self, config: WorkerConfig) -> bool:
        with self._workers_lock:
            if any(worker.config.root == config.root for worker in self.workers):
                return False
            worker = self._make_worker(config)
            worker._ensure_dirs()
            self.workers.append(worker)
        self._notify_status()
        return True

    def remove_root(self, root: Path) -> bool:
        resolved = root.resolve()
        with self._workers_lock:
            if len(self.workers) <= 1:
                return False
            before = len(self.workers)
            self.workers = [worker for worker in self.workers if worker.config.root != resolved]
            removed = len(self.workers) != before
        if removed:
            self._drop_queued_jobs_for_root(resolved)
            self._notify_status()
        return removed

    def _drop_queued_jobs_for_root(self, root: Path) -> None:
        kept: list[ConversionJob] = []
        while True:
            try:
                job = self.documents.get_nowait()
            except queue.Empty:
                break
            if job.root != root:
                kept.append(job)
            self.documents.task_done()
        for job in kept:
            self.documents.put(job)

    def set_ollama_model(self, model: str | None) -> None:
        with self._workers_lock:
            self.workers = [
                MarkerPdfWorker(
                    WorkerConfig(
                        root=worker.config.root,
                        incoming_dir=worker.config.incoming_dir,
                        processing_dir=worker.config.processing_dir,
                        converted_dir=worker.config.converted_dir,
                        failed_dir=worker.config.failed_dir,
                        poll_interval=worker.config.poll_interval,
                        stable_seconds=worker.config.stable_seconds,
                        marker_command=worker.config.marker_command,
                        marker_timeout=worker.config.marker_timeout,
                        ollama_model=model,
                        use_ollama=model is not None,
                    ),
                    self.documents,
                    self.stop_event,
                    self._record_progress,
                )
                for worker in self.workers
            ]
        self._notify_status()

    def roots(self) -> list[Path]:
        return [worker.config.root for worker in self.worker_snapshot()]

    def status(self) -> WorkerStatus:
        current_job = self.current_job
        return WorkerStatus(
            roots=tuple(self.roots()),
            queue_size=self.documents.qsize(),
            current_document=current_job.source.name if current_job else None,
            current_root=current_job.root if current_job else None,
            stopping=self.stop_event.is_set(),
            last_message=self.last_message,
        )

    def add_status_listener(self, listener: Callable[[WorkerStatus], None]) -> None:
        self._status_listeners.append(listener)

    def _notify_status(self) -> None:
        status = self.status()
        for listener in list(self._status_listeners):
            listener(status)


def user_config_path() -> Path:
    return Path.home() / ".marker-pdf-agent" / DEFAULT_CONFIG_NAME


def agent_config_path(args: argparse.Namespace) -> Path:
    value = getattr(args, "config", None)
    return Path(value).expanduser().resolve() if value else user_config_path()


def load_monitored_roots(path: Path) -> list[Path]:
    return load_agent_config(path)[0]


def load_agent_config(path: Path) -> tuple[list[Path], str | None]:
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid marker-pdf-agent config at {path}: {exc}") from exc

    roots = data.get("roots", [])
    if not isinstance(roots, list):
        raise RuntimeError(f"invalid marker-pdf-agent config at {path}: roots must be a list")
    ollama_model = data.get("ollama_model")
    if ollama_model is not None and not isinstance(ollama_model, str):
        raise RuntimeError(f"invalid marker-pdf-agent config at {path}: ollama_model must be a string or null")
    return (
        unique_roots(Path(root).expanduser().resolve() for root in roots if isinstance(root, str)),
        ollama_model or None,
    )


def save_monitored_roots(path: Path, roots: Iterable[Path]) -> None:
    _existing_roots, ollama_model = load_agent_config(path)
    save_agent_config(path, roots, ollama_model)


def save_agent_config(path: Path, roots: Iterable[Path], ollama_model: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "roots": [str(root) for root in unique_roots(root.resolve() for root in roots)],
        "ollama_model": ollama_model,
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def unique_roots(roots: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        unique.append(root)
    return unique


def build_config_for_root(args: argparse.Namespace, root: Path) -> WorkerConfig:
    namespace = argparse.Namespace(**vars(args))
    namespace.root = str(root)
    return build_config(namespace)


def build_tray_configs(args: argparse.Namespace) -> tuple[Path, list[WorkerConfig]]:
    path = agent_config_path(args)
    roots, saved_ollama_model = load_agent_config(path)
    explicit_root = Path(args.root).expanduser().resolve()
    roots = unique_roots([explicit_root, *roots])
    if args.no_ollama:
        args.ollama_model = None
    elif args.ollama_model is None and saved_ollama_model:
        args.ollama_model = saved_ollama_model
    save_agent_config(path, roots, args.ollama_model)
    return path, [build_config_for_root(args, root) for root in roots]


def run_tray(args: argparse.Namespace) -> None:
    config_path, configs = build_tray_configs(args)
    manager = WorkerManager(configs)
    from marker_pdf_agent.tray import run_tray_app

    try:
        run_tray_app(manager, args, config_path)
    except RuntimeError as exc:
        if not is_gui_dependency_error(exc):
            raise
        show_error_box("Marker PDF Agent", GUI_DEPENDENCY_ERROR)
        print(GUI_DEPENDENCY_ERROR, file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


def is_gui_dependency_error(exc: RuntimeError) -> bool:
    return "install GUI dependencies" in str(exc)


def show_error_box(title: str, message: str) -> None:
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(
                ["osascript", "-e", f"display alert {json.dumps(title)} message {json.dumps(message)} as critical"],
                check=False,
            )
            return
        if system == "Windows":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # type: ignore[attr-defined]
            return

        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:
        return


def build_config(args: argparse.Namespace) -> WorkerConfig:
    root = Path(args.root).resolve()
    incoming_dir = (root / args.incoming).resolve()
    converted_dir = (root / args.converted).resolve()
    processing_dir = (root / ".marker-pdf-agent" / "processing").resolve()
    failed_dir = (root / ".marker-pdf-agent" / "failed").resolve()
    ollama_model = args.ollama_model if args.ollama_model and not args.no_ollama else None
    return WorkerConfig(
        root=root,
        incoming_dir=incoming_dir,
        processing_dir=processing_dir,
        converted_dir=converted_dir,
        failed_dir=failed_dir,
        poll_interval=args.poll_interval,
        stable_seconds=args.stable_seconds,
        marker_command=args.marker_command,
        marker_timeout=args.marker_timeout,
        ollama_model=ollama_model,
        use_ollama=ollama_model is not None,
    )


def add_worker_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=os.getcwd(), help="Folder to manage. Defaults to the launch directory.")
    parser.add_argument("--config", help="Path to the persisted foreground GUI configuration file.")
    parser.add_argument("--incoming", default="incoming", help="Subfolder to watch for moved-in documents.")
    parser.add_argument(
        "--converted", default="converted", help="Subfolder where routed markdown artifacts are placed."
    )
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between scans.")
    parser.add_argument(
        "--stable-seconds", type=float, default=1.0, help="Seconds a file must remain unchanged before queuing."
    )
    parser.add_argument("--marker-command", default="marker_single", help="marker-pdf CLI executable to run.")
    parser.add_argument(
        "--marker-timeout", type=float, default=1800.0, help="Maximum seconds to allow one marker-pdf conversion."
    )
    parser.add_argument("--ollama-model", help="Enable Ollama folder routing with this model.")
    parser.add_argument(
        "--no-ollama",
        action="store_true",
        help="Disable Ollama folder routing. This is the default unless --ollama-model is set.",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"run", "tray", "install-launcher", "install-service", "uninstall-service", "status"}
    if not args or args[0] not in commands:
        parser = argparse.ArgumentParser(
            description="Convert newly dropped documents with marker-pdf in a background queue."
        )
        add_worker_arguments(parser)
        parser.add_argument("--tray", action="store_true", help="Run with the optional foreground status-bar GUI.")
        parsed = parser.parse_args(args)
        parsed.command = "tray" if parsed.tray else "run"
        parsed.service_name = DEFAULT_SERVICE_NAME
        return parsed

    parser = argparse.ArgumentParser(
        description="Convert newly dropped documents with marker-pdf in a background queue."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run the foreground worker.")
    add_worker_arguments(run_parser)
    run_parser.add_argument("--tray", action="store_true", help="Run with the optional foreground status-bar GUI.")
    run_parser.set_defaults(service_name=DEFAULT_SERVICE_NAME)

    tray_parser = subparsers.add_parser("tray", help="Run the foreground status-bar GUI.")
    add_worker_arguments(tray_parser)
    tray_parser.set_defaults(service_name=DEFAULT_SERVICE_NAME, tray=True)

    install_parser = subparsers.add_parser(
        "install-service", help="Install a platform-specific background service definition."
    )
    add_worker_arguments(install_parser)
    install_parser.add_argument(
        "--service-name", default=DEFAULT_SERVICE_NAME, help="Service name or label to install."
    )

    launcher_parser = subparsers.add_parser(
        "install-launcher", help="Create a platform-specific launcher in the managed folder."
    )
    add_worker_arguments(launcher_parser)
    launcher_parser.add_argument(
        "--launcher-name", default="Marker PDF Agent", help="Display name for the generated launcher."
    )
    launcher_parser.set_defaults(service_name=DEFAULT_SERVICE_NAME)

    uninstall_parser = subparsers.add_parser(
        "uninstall-service", help="Remove the platform-specific service definition."
    )
    uninstall_parser.add_argument(
        "--root", default=os.getcwd(), help="Folder managed by the service. Used for Windows instruction cleanup."
    )
    uninstall_parser.add_argument(
        "--service-name", default=DEFAULT_SERVICE_NAME, help="Service name or label to remove."
    )

    status_parser = subparsers.add_parser("status", help="Show status for the platform-specific service backend.")
    status_parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME, help="Service name or label to inspect.")
    return parser.parse_args(args)


def main() -> None:
    args = parse_args()
    if args.command == "run" and getattr(args, "tray", False):
        args.command = "tray"
    if args.command == "install-service":
        install_service(args)
        return
    if args.command == "install-launcher":
        install_launcher(args)
        return
    if args.command == "uninstall-service":
        uninstall_service(args)
        return
    if args.command == "status":
        service_status(args)
        return

    with SingletonLock(singleton_lock_path()):
        if args.command == "tray":
            run_tray(args)
            return

        config = build_config(args)
        manager = WorkerManager([config])

        def stop(_signum: int, _frame: object) -> None:
            manager.stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        manager.run()


if __name__ == "__main__":
    main()
