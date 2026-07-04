from __future__ import annotations

import argparse
import os
import platform
import plistlib
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

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
FAST_OLLAMA_MODELS = ("llama3.1", "llama3", "mistral", "phi3", "gemma2")
SYSTEM_FOLDERS = {"incoming", "processing", "converted", "failed", ".marker-pdf-agent"}
DEFAULT_SERVICE_NAME = "marker-pdf-agent"


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
    config: WorkerConfig


class MarkerPdfWorker:
    def __init__(
        self,
        config: WorkerConfig,
        documents: queue.Queue[ConversionJob] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.documents: queue.Queue[ConversionJob] = documents or queue.Queue()
        self.stop_event = stop_event or threading.Event()
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
                self.documents.put(ConversionJob(path, self.config))
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
        folder_name = ask_ollama_for_folder(self.config.ollama_model, existing_folders, markdown_text)
        if not folder_name:
            return self.config.converted_dir / "uncategorized"
        return self.config.converted_dir / sanitize_folder_name(folder_name)

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


def discover_ollama_model(preferred: str | None) -> str | None:
    if shutil.which("ollama") is None:
        return None
    if preferred:
        return preferred

    try:
        result = subprocess.run(["ollama", "list"], check=True, capture_output=True, text=True)
    except subprocess.SubprocessError:
        return None

    installed = result.stdout.lower()
    for model in FAST_OLLAMA_MODELS:
        if model in installed:
            return model
    return None


def ask_ollama_for_folder(model: str, existing_folders: Iterable[str], markdown_text: str) -> str | None:
    existing_folder_names = sorted(existing_folders)
    folder_list = ", ".join(existing_folder_names) or "none"
    prompt = (
        "You route converted documents into concise folder names. "
        "Choose the best destination based on both the converted document content and the current subfolder structure. "
        "Use an existing converted/ subfolder when it fits, or propose one new folder that fits naturally beside the current subfolders. "
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
    except subprocess.SubprocessError:
        return None

    first_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    return choose_folder_from_ollama_response(first_line, existing_folder_names) if first_line else None


def choose_folder_from_ollama_response(response: str, existing_folders: Iterable[str]) -> str | None:
    existing = {sanitize_folder_name(folder) for folder in existing_folders}
    candidates = [sanitize_folder_name(part) for part in re.split(r"[/\\,;>|]+", response)]
    candidates = [candidate for candidate in candidates if candidate]
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
    command = [sys.executable, "-m", "marker_pdf_agent.worker", "run", "--root", str(Path(args.root).resolve())]
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
        print(f"Start it with: systemctl --user daemon-reload && systemctl --user enable --now {args.service_name}.service", flush=True)
        return
    if system == "Windows":
        path = install_windows_service_instructions(args)
        print(f"Wrote Windows service setup instructions to {path}", flush=True)
        return
    raise RuntimeError(f"unsupported platform for service install: {system}")


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
        print("Check the service manager you used to install the Windows service, such as NSSM or Services.msc.", flush=True)
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


def launchd_plist_path(name: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{service_label(name)}.plist"


def systemd_user_service_path(name: str) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{name}.service"


def windows_service_instruction_path(root: Path) -> Path:
    return service_state_dir(root) / "windows-service.md"


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

    def __enter__(self) -> "SingletonLock":
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
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
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
        self.workers = [MarkerPdfWorker(config, self.documents, self.stop_event) for config in configs]

    def run(self) -> None:
        for worker in self.workers:
            worker._ensure_dirs()
        converter = threading.Thread(target=self._convert_loop, name="marker-converter")
        converter.start()
        watched = ", ".join(str(worker.config.incoming_dir) for worker in self.workers)
        print(f"Watching {watched} for documents. Press Ctrl+C to stop.", flush=True)

        try:
            while not self.stop_event.is_set():
                for worker in self.workers:
                    worker._scan_once()
                time.sleep(min(worker.config.poll_interval for worker in self.workers))
        finally:
            self.stop_event.set()
            converter.join()

    def _convert_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                job = self.documents.get(timeout=0.5)
            except queue.Empty:
                continue

            worker = self._worker_for(job.config)
            try:
                worker._process_document(job.source)
            except Exception as exc:  # noqa: BLE001 - keep manager alive after per-file failures.
                print(f"Failed to process {job.source.name}: {exc}", flush=True)
                worker._move_to_failed(job.source)
            finally:
                worker.seen.discard(job.source)
                self.documents.task_done()

    def _worker_for(self, config: WorkerConfig) -> MarkerPdfWorker:
        for worker in self.workers:
            if worker.config == config:
                return worker
        raise RuntimeError(f"no worker registered for {config.root}")


def build_config(args: argparse.Namespace) -> WorkerConfig:
    root = Path(args.root).resolve()
    incoming_dir = (root / args.incoming).resolve()
    converted_dir = (root / args.converted).resolve()
    processing_dir = (root / ".marker-pdf-agent" / "processing").resolve()
    failed_dir = (root / ".marker-pdf-agent" / "failed").resolve()
    ollama_model = discover_ollama_model(args.ollama_model) if not args.no_ollama else None
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
        use_ollama=not args.no_ollama,
    )


def add_worker_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=os.getcwd(), help="Folder to manage. Defaults to the launch directory.")
    parser.add_argument("--incoming", default="incoming", help="Subfolder to watch for moved-in documents.")
    parser.add_argument("--converted", default="converted", help="Subfolder where routed markdown artifacts are placed.")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between scans.")
    parser.add_argument("--stable-seconds", type=float, default=1.0, help="Seconds a file must remain unchanged before queuing.")
    parser.add_argument("--marker-command", default="marker_single", help="marker-pdf CLI executable to run.")
    parser.add_argument("--marker-timeout", type=float, default=1800.0, help="Maximum seconds to allow one marker-pdf conversion.")
    parser.add_argument("--ollama-model", help="Specific Ollama model to use for folder routing.")
    parser.add_argument("--no-ollama", action="store_true", help="Disable Ollama folder routing.")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"run", "install-service", "uninstall-service", "status"}
    if not args or args[0] not in commands:
        parser = argparse.ArgumentParser(description="Convert newly dropped documents with marker-pdf in a background queue.")
        add_worker_arguments(parser)
        parsed = parser.parse_args(args)
        parsed.command = "run"
        parsed.service_name = DEFAULT_SERVICE_NAME
        return parsed

    parser = argparse.ArgumentParser(description="Convert newly dropped documents with marker-pdf in a background queue.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run the foreground worker.")
    add_worker_arguments(run_parser)
    run_parser.set_defaults(service_name=DEFAULT_SERVICE_NAME)

    install_parser = subparsers.add_parser("install-service", help="Install a platform-specific background service definition.")
    add_worker_arguments(install_parser)
    install_parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME, help="Service name or label to install.")

    uninstall_parser = subparsers.add_parser("uninstall-service", help="Remove the platform-specific service definition.")
    uninstall_parser.add_argument("--root", default=os.getcwd(), help="Folder managed by the service. Used for Windows instruction cleanup.")
    uninstall_parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME, help="Service name or label to remove.")

    status_parser = subparsers.add_parser("status", help="Show status for the platform-specific service backend.")
    status_parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME, help="Service name or label to inspect.")
    return parser.parse_args(args)


def main() -> None:
    args = parse_args()
    if args.command == "install-service":
        install_service(args)
        return
    if args.command == "uninstall-service":
        uninstall_service(args)
        return
    if args.command == "status":
        service_status(args)
        return

    with SingletonLock(singleton_lock_path()):
        config = build_config(args)
        manager = WorkerManager([config])

        def stop(_signum: int, _frame: object) -> None:
            manager.stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        manager.run()


if __name__ == "__main__":
    main()
