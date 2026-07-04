from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

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
    ollama_model: str | None
    use_ollama: bool


class MarkerPdfWorker:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.documents: queue.Queue[Path] = queue.Queue()
        self.stop_event = threading.Event()
        self.seen: set[Path] = set()

    def run(self) -> None:
        self._ensure_dirs()
        worker = threading.Thread(target=self._convert_loop, name="marker-converter", daemon=True)
        worker.start()
        print(f"Watching {self.config.incoming_dir} for documents. Press Ctrl+C to stop.", flush=True)

        try:
            while not self.stop_event.is_set():
                self._scan_once()
                time.sleep(self.config.poll_interval)
        finally:
            self.stop_event.set()
            worker.join(timeout=5)

    def _ensure_dirs(self) -> None:
        for directory in (
            self.config.incoming_dir,
            self.config.processing_dir,
            self.config.converted_dir,
            self.config.failed_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def _scan_once(self) -> None:
        for path in sorted(self.config.incoming_dir.iterdir()):
            if not path.is_file() or path in self.seen or path.suffix.lower() not in DOCUMENT_EXTENSIONS:
                continue
            if self._is_stable(path):
                self.seen.add(path)
                self.documents.put(path)

    def _is_stable(self, path: Path) -> bool:
        try:
            first_size = path.stat().st_size
            first_mtime = path.stat().st_mtime
            time.sleep(self.config.stable_seconds)
            second = path.stat()
        except FileNotFoundError:
            return False
        return first_size == second.st_size and first_mtime == second.st_mtime

    def _convert_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                source = self.documents.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                self._process_document(source)
            except Exception as exc:  # noqa: BLE001 - keep worker alive after per-file failures.
                print(f"Failed to process {source.name}: {exc}", flush=True)
                self._move_to_failed(source)
            finally:
                self.documents.task_done()

    def _process_document(self, source: Path) -> None:
        processing_source = self._move_to_processing(source)
        with tempfile.TemporaryDirectory(prefix="marker-output-") as temp_name:
            output_dir = Path(temp_name)
            self._run_marker(processing_source, output_dir)
            markdown_files = sorted(output_dir.rglob("*.md"))
            if not markdown_files:
                raise RuntimeError("marker-pdf did not produce a markdown file")

            primary_markdown = markdown_files[0]
            destination_folder = self._choose_destination(primary_markdown)
            destination_folder.mkdir(parents=True, exist_ok=True)
            artifact = self._pack_or_select_artifact(output_dir, primary_markdown, processing_source.stem)
            destination = unique_path(destination_folder / artifact.name)
            shutil.move(str(artifact), destination)
            processing_source.unlink(missing_ok=True)
            print(f"Converted {source.name} -> {destination.relative_to(self.config.root)}", flush=True)

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
            subprocess.run(command, check=True)
        except FileNotFoundError as exc:
            raise RuntimeError(f"marker command not found: {self.config.marker_command}") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"marker-pdf failed with exit code {exc.returncode}") from exc

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
    folder_list = ", ".join(sorted(existing_folders)) or "none"
    prompt = (
        "You route converted documents into concise folder names. "
        "Use an existing folder when it fits, or propose one new folder. "
        "Return only the folder name, no punctuation or explanation.\n\n"
        f"Existing folders: {folder_list}\n\n"
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
    return sanitize_folder_name(first_line) if first_line else None


def sanitize_folder_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", value).strip().strip(".")
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
        ollama_model=ollama_model,
        use_ollama=not args.no_ollama,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert newly dropped documents with marker-pdf in a background queue.")
    parser.add_argument("--root", default=os.getcwd(), help="Folder to manage. Defaults to the launch directory.")
    parser.add_argument("--incoming", default="incoming", help="Subfolder to watch for moved-in documents.")
    parser.add_argument("--converted", default="converted", help="Subfolder where routed markdown artifacts are placed.")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between scans.")
    parser.add_argument("--stable-seconds", type=float, default=1.0, help="Seconds a file must remain unchanged before queuing.")
    parser.add_argument("--marker-command", default="marker_single", help="marker-pdf CLI executable to run.")
    parser.add_argument("--ollama-model", help="Specific Ollama model to use for folder routing.")
    parser.add_argument("--no-ollama", action="store_true", help="Disable Ollama folder routing.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = build_config(args)
    worker = MarkerPdfWorker(config)

    def stop(_signum: int, _frame: object) -> None:
        worker.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run()


if __name__ == "__main__":
    main()
