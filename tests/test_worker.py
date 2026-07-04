from __future__ import annotations

import subprocess
from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from marker_pdf_agent.worker import (
    ConversionJob,
    MarkerPdfWorker,
    SingletonLock,
    WorkerConfig,
    WorkerManager,
    ask_ollama_for_folder,
    build_config,
    build_tray_configs,
    install_launchd_service,
    install_systemd_user_service,
    install_windows_service_instructions,
    list_ollama_models,
    load_agent_config,
    load_monitored_roots,
    parse_args,
    run_tray,
    sanitize_folder_name,
    save_agent_config,
    save_monitored_roots,
    service_label,
    service_run_arguments,
    singleton_lock_path,
    unique_path,
)


def make_config(tmp_path: Path, *, use_ollama: bool = False, ollama_model: str | None = None) -> WorkerConfig:
    return WorkerConfig(
        root=tmp_path,
        incoming_dir=tmp_path / "incoming",
        processing_dir=tmp_path / ".marker-pdf-agent" / "processing",
        converted_dir=tmp_path / "converted",
        failed_dir=tmp_path / ".marker-pdf-agent" / "failed",
        poll_interval=0.01,
        stable_seconds=0,
        marker_command="marker_single",
        marker_timeout=1800.0,
        ollama_model=ollama_model,
        use_ollama=use_ollama,
    )


def test_pack_or_select_artifact_returns_markdown_when_no_assets(tmp_path: Path) -> None:
    worker = MarkerPdfWorker(make_config(tmp_path))
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    markdown_file = output_dir / "document.md"
    markdown_file.write_text("# Title\n", encoding="utf-8")

    artifact = worker._pack_or_select_artifact(output_dir, markdown_file, "source")

    assert artifact == output_dir / "source.md"
    assert artifact.read_text(encoding="utf-8") == "# Title\n"


def test_pack_or_select_artifact_returns_zip_when_assets_exist(tmp_path: Path) -> None:
    import zipfile

    worker = MarkerPdfWorker(make_config(tmp_path))
    output_dir = tmp_path / "output"
    image_dir = output_dir / "images"
    image_dir.mkdir(parents=True)
    markdown_file = output_dir / "document.md"
    markdown_file.write_text("![scan](images/scan.png)\n", encoding="utf-8")
    (image_dir / "scan.png").write_bytes(b"png")

    artifact = worker._pack_or_select_artifact(output_dir, markdown_file, "source")

    assert artifact == output_dir / "source.zip"
    with zipfile.ZipFile(artifact) as archive:
        assert sorted(archive.namelist()) == ["document.md", "images/scan.png"]


def test_choose_destination_uses_uncategorized_without_ollama(tmp_path: Path) -> None:
    config = make_config(tmp_path, use_ollama=False)
    config.converted_dir.mkdir()
    markdown_file = tmp_path / "document.md"
    markdown_file.write_text("# Tax Records\n", encoding="utf-8")

    worker = MarkerPdfWorker(config)

    assert worker._choose_destination(markdown_file) == tmp_path / "converted" / "uncategorized"


def test_choose_destination_uses_ollama_folder(monkeypatch, tmp_path: Path) -> None:
    config = make_config(tmp_path, use_ollama=True, ollama_model="llama3.1")
    (config.converted_dir / "finance").mkdir(parents=True)
    markdown_file = tmp_path / "document.md"
    markdown_file.write_text("# Invoice\nTotal due next week", encoding="utf-8")

    def fake_ollama(model: str, existing_folders: list[str], markdown_text: str) -> tuple[str, None]:
        assert model == "llama3.1"
        assert existing_folders == ["finance"]
        assert "Invoice" in markdown_text
        return "Finance", None

    monkeypatch.setattr("marker_pdf_agent.worker.ask_ollama_for_folder", fake_ollama)
    worker = MarkerPdfWorker(config)

    assert worker._choose_destination(markdown_file) == tmp_path / "converted" / "finance"


def test_choose_destination_rejects_reserved_ollama_folder(monkeypatch, tmp_path: Path) -> None:
    config = make_config(tmp_path, use_ollama=True, ollama_model="llama3.1")
    config.converted_dir.mkdir()
    markdown_file = tmp_path / "document.md"
    markdown_file.write_text("# Notes\n", encoding="utf-8")

    monkeypatch.setattr("marker_pdf_agent.worker.ask_ollama_for_folder", lambda *_args: ("converted", None))
    worker = MarkerPdfWorker(config)

    assert worker._choose_destination(markdown_file) == tmp_path / "converted" / "uncategorized"


def test_process_document_reports_progress(monkeypatch, capsys, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    worker = MarkerPdfWorker(config)
    worker._ensure_dirs()
    source = config.incoming_dir / "source.pdf"
    source.write_bytes(b"pdf")

    def fake_run_marker(_source: Path, output_dir: Path) -> None:
        (output_dir / "source.md").write_text("# Converted\n", encoding="utf-8")

    monkeypatch.setattr(worker, "_run_marker", fake_run_marker)

    worker._process_document(source)

    stdout = capsys.readouterr().out
    assert "Processing source.pdf" in stdout
    assert "Converting source.pdf with marker_single" in stdout
    assert "Routing source.pdf" in stdout
    assert "Packaging source.pdf" in stdout
    assert "Converted source.pdf -> converted/uncategorized/source.md" in stdout


def test_process_document_keeps_original_with_converted_artifact(monkeypatch, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    worker = MarkerPdfWorker(config)
    worker._ensure_dirs()
    source = config.incoming_dir / "source.pdf"
    source.write_bytes(b"original pdf")

    def fake_run_marker(_source: Path, output_dir: Path) -> None:
        (output_dir / "source.md").write_text("# Converted\n", encoding="utf-8")

    monkeypatch.setattr(worker, "_run_marker", fake_run_marker)

    worker._process_document(source)

    destination_folder = config.converted_dir / "uncategorized"
    assert (destination_folder / "source.pdf").read_bytes() == b"original pdf"
    assert (destination_folder / "source.md").read_text(encoding="utf-8") == "# Converted\n"
    assert not source.exists()


def test_process_document_moves_processing_source_to_failed_on_conversion_error(monkeypatch, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    worker = MarkerPdfWorker(config)
    worker._ensure_dirs()
    source = config.incoming_dir / "source.pdf"
    source.write_bytes(b"original pdf")

    def fake_run_marker(_source: Path, _output_dir: Path) -> None:
        raise RuntimeError("marker-pdf failed with exit code 2")

    monkeypatch.setattr(worker, "_run_marker", fake_run_marker)

    with pytest.raises(RuntimeError, match="marker-pdf failed"):
        worker._process_document(source)

    assert (config.failed_dir / "source.pdf").read_bytes() == b"original pdf"
    assert not source.exists()
    assert not (config.processing_dir / "source.pdf").exists()


def test_ensure_dirs_moves_orphaned_processing_files_to_failed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    processing_file = config.processing_dir / "orphan.pdf"
    processing_file.parent.mkdir(parents=True)
    processing_file.write_bytes(b"partial conversion")

    MarkerPdfWorker(config)._ensure_dirs()

    assert (config.failed_dir / "orphan.pdf").read_bytes() == b"partial conversion"
    assert not processing_file.exists()


def test_convert_loop_allows_same_filename_retry_after_failure(monkeypatch, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manager = WorkerManager([config])
    worker = manager.workers[0]
    source = config.incoming_dir / "source.pdf"
    worker.seen.add(source)
    manager.documents.put(ConversionJob(source, config.root))

    def fake_process_document(_self: MarkerPdfWorker, _source: Path) -> None:
        manager.stop_event.set()
        raise RuntimeError("conversion failed")

    monkeypatch.setattr(MarkerPdfWorker, "_process_document", fake_process_document)

    manager._convert_loop()

    assert source not in worker.seen
    assert manager.documents.unfinished_tasks == 0


def test_worker_manager_processes_multiple_roots_through_one_queue(monkeypatch, tmp_path: Path) -> None:
    first_config = make_config(tmp_path / "first")
    second_config = make_config(tmp_path / "second")
    first_source = first_config.incoming_dir / "first.pdf"
    second_source = second_config.incoming_dir / "second.pdf"
    manager = WorkerManager([first_config, second_config])
    manager.documents.put(ConversionJob(first_source, first_config.root))
    manager.documents.put(ConversionJob(second_source, second_config.root))
    started: list[str] = []
    finished: list[str] = []

    def fake_process_document(self: MarkerPdfWorker, source: Path) -> None:
        started.append(source.name)
        assert finished == started[:-1]
        finished.append(source.name)
        if len(finished) == 2:
            manager.stop_event.set()

    monkeypatch.setattr(MarkerPdfWorker, "_process_document", fake_process_document)

    manager._convert_loop()

    assert started == ["first.pdf", "second.pdf"]
    assert finished == ["first.pdf", "second.pdf"]
    assert manager.documents.unfinished_tasks == 0


def test_worker_manager_notifies_status_when_job_changes(monkeypatch, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    source = config.incoming_dir / "source.pdf"
    manager = WorkerManager([config])
    manager.documents.put(ConversionJob(source, config.root))
    statuses: list[str | None] = []
    manager.add_status_listener(lambda status: statuses.append(status.current_document))

    def fake_process_document(_self: MarkerPdfWorker, _source: Path) -> None:
        manager.stop_event.set()

    monkeypatch.setattr(MarkerPdfWorker, "_process_document", fake_process_document)

    manager._convert_loop()

    assert statuses == ["source.pdf", None]


def test_worker_manager_uses_current_config_for_queued_job(monkeypatch, tmp_path: Path) -> None:
    config = make_config(tmp_path, use_ollama=False)
    manager = WorkerManager([config])
    source = config.incoming_dir / "source.pdf"
    manager.documents.put(ConversionJob(source, config.root))
    models: list[str | None] = []

    def fake_process_document(self: MarkerPdfWorker, _source: Path) -> None:
        models.append(self.config.ollama_model)
        manager.stop_event.set()

    monkeypatch.setattr(MarkerPdfWorker, "_process_document", fake_process_document)

    manager.set_ollama_model("llama3.1:latest")
    manager._convert_loop()

    assert models == ["llama3.1:latest"]


def test_worker_manager_skips_queued_job_after_root_removed(monkeypatch, tmp_path: Path) -> None:
    first_config = make_config(tmp_path / "first")
    second_config = make_config(tmp_path / "second")
    manager = WorkerManager([first_config, second_config])
    source = second_config.incoming_dir / "source.pdf"
    manager.documents.put(ConversionJob(source, second_config.root))
    processed: list[Path] = []

    def fake_process_document(_self: MarkerPdfWorker, source_path: Path) -> None:
        processed.append(source_path)

    monkeypatch.setattr(MarkerPdfWorker, "_process_document", fake_process_document)

    assert manager.remove_root(second_config.root) is True

    assert processed == []
    assert manager.documents.unfinished_tasks == 0


def test_worker_manager_adds_and_removes_roots(tmp_path: Path) -> None:
    first_config = make_config(tmp_path / "first")
    second_config = make_config(tmp_path / "second")
    manager = WorkerManager([first_config])

    assert manager.add_config(second_config) is True
    assert manager.add_config(second_config) is False
    assert manager.status().roots == (first_config.root, second_config.root)
    assert manager.remove_root(second_config.root) is True
    assert manager.remove_root(first_config.root) is False
    assert manager.status().roots == (first_config.root,)


def test_worker_manager_updates_ollama_model_for_all_roots(tmp_path: Path) -> None:
    first_config = make_config(tmp_path / "first")
    second_config = make_config(tmp_path / "second")
    manager = WorkerManager([first_config, second_config])

    manager.set_ollama_model("llama3.1:latest")

    assert [worker.config.ollama_model for worker in manager.worker_snapshot()] == [
        "llama3.1:latest",
        "llama3.1:latest",
    ]
    assert all(worker.config.use_ollama for worker in manager.worker_snapshot())

    manager.set_ollama_model(None)

    assert [worker.config.ollama_model for worker in manager.worker_snapshot()] == [None, None]
    assert not any(worker.config.use_ollama for worker in manager.worker_snapshot())


def test_run_marker_times_out_and_terminates_process(monkeypatch, tmp_path: Path) -> None:
    config = WorkerConfig(**{**make_config(tmp_path).__dict__, "marker_timeout": 0.01})
    worker = MarkerPdfWorker(config)
    process = FakeProcess(returncode_after_polls=None)

    monkeypatch.setattr("marker_pdf_agent.worker.subprocess.Popen", lambda _command: process)
    monkeypatch.setattr("marker_pdf_agent.worker.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="timed out"):
        worker._run_marker(tmp_path / "source.pdf", tmp_path / "output")

    assert process.terminated


def test_run_marker_interrupts_process_when_worker_stops(monkeypatch, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    worker = MarkerPdfWorker(config)
    worker.stop_event.set()
    process = FakeProcess(returncode_after_polls=None)

    monkeypatch.setattr("marker_pdf_agent.worker.subprocess.Popen", lambda _command: process)

    with pytest.raises(RuntimeError, match="interrupted by shutdown"):
        worker._run_marker(tmp_path / "source.pdf", tmp_path / "output")

    assert process.terminated


def test_run_marker_raises_on_nonzero_exit(monkeypatch, tmp_path: Path) -> None:
    config = make_config(tmp_path)
    worker = MarkerPdfWorker(config)
    process = FakeProcess(returncode_after_polls=0, final_returncode=2)

    monkeypatch.setattr("marker_pdf_agent.worker.subprocess.Popen", lambda _command: process)

    with pytest.raises(RuntimeError, match="exit code 2"):
        worker._run_marker(tmp_path / "source.pdf", tmp_path / "output")


def test_related_documents_create_expected_final_folder_structure(monkeypatch, tmp_path: Path) -> None:
    config = make_config(tmp_path, use_ollama=True, ollama_model="llama3.1")
    worker = MarkerPdfWorker(config)
    worker._ensure_dirs()
    documents = {
        "tufte-latex-guide.pdf": "# Tufte LaTeX Documentation\nGuide to handouts and books.",
        "tufte-r-markdown.pdf": "# Tufte R Markdown Styles\nR Markdown formats for Tufte-style documents.",
        "invoice-january.pdf": "# Invoice January\nAmount due for consulting services.",
        "invoice-february.pdf": "# Invoice February\nAmount due for consulting services.",
        "lease-agreement.pdf": "# Apartment Lease\nTerms, tenant, landlord, and rent.",
        "resume.pdf": "# Resume\nExperience, projects, and education.",
    }
    expected_routes = {
        "tufte-latex-guide.pdf": "tufte-documentation",
        "tufte-r-markdown.pdf": "tufte-documentation",
        "invoice-january.pdf": "invoices",
        "invoice-february.pdf": "invoices",
        "lease-agreement.pdf": "legal",
        "resume.pdf": "career",
    }

    for name in documents:
        (config.incoming_dir / name).write_bytes(b"%PDF-1.4\n% small fixture\n")

    def fake_run_marker(source: Path, output_dir: Path) -> None:
        output_dir.joinpath(f"{source.stem}.md").write_text(documents[source.name], encoding="utf-8")

    def fake_ollama(_model: str, existing_folders: list[str], markdown_text: str) -> tuple[str, None]:
        if "Tufte" in markdown_text:
            return "tufte-documentation", None
        if "Invoice" in markdown_text:
            return "invoices", None
        if "Lease" in markdown_text:
            return "legal", None
        if "Resume" in markdown_text:
            return "career", None
        return "uncategorized", None

    monkeypatch.setattr(worker, "_run_marker", fake_run_marker)
    monkeypatch.setattr("marker_pdf_agent.worker.ask_ollama_for_folder", fake_ollama)

    for source in sorted(config.incoming_dir.iterdir()):
        worker._process_document(source)

    actual_tree = sorted(
        str(path.relative_to(config.converted_dir)) for path in config.converted_dir.rglob("*") if path.is_file()
    )
    expected_tree = sorted(
        file_name
        for source_name, folder in expected_routes.items()
        for file_name in (
            f"{folder}/{source_name}",
            f"{folder}/{Path(source_name).stem}.md",
        )
    )

    assert actual_tree == expected_tree
    assert not any(config.incoming_dir.iterdir())


def test_ollama_prompt_uses_document_content_and_converted_structure(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        captured["command"] = command
        return CompletedProcess(command, 0, stdout="tax-records\n", stderr="")

    monkeypatch.setattr("marker_pdf_agent.worker.subprocess.run", fake_run)

    folder, error = ask_ollama_for_folder("llama3.1", ["finance", "legal"], "# Tax return\nW-2 income")

    assert folder == "tax-records"
    assert error is None
    prompt = captured["command"][3]
    assert "based on both the converted document content and the current subfolder structure" in prompt
    assert "Current converted/ subfolders: finance, legal" in prompt
    assert "# Tax return" in prompt


def test_ollama_path_like_response_reuses_existing_top_level_folder(monkeypatch) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(
            command,
            0,
            stdout="tufte-latex-documentation/tufte-r-markdown-styles\n",
            stderr="",
        )

    monkeypatch.setattr("marker_pdf_agent.worker.subprocess.run", fake_run)

    folder, error = ask_ollama_for_folder(
        "llama3.1",
        ["tufte-latex-documentation"],
        "# Tufte R Markdown Styles\nDocumentation for Tufte-style documents",
    )

    assert folder == "tufte-latex-documentation"
    assert error is None


def test_ollama_path_like_response_ignores_converted_prefix(monkeypatch) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(command, 0, stdout="converted/invoices\n", stderr="")

    monkeypatch.setattr("marker_pdf_agent.worker.subprocess.run", fake_run)

    folder, error = ask_ollama_for_folder("llama3.1", ["finance"], "# Invoice\nAmount due")

    assert folder == "invoices"
    assert error is None


def test_ollama_failure_returns_error_message(monkeypatch) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command, stderr="model not found")

    monkeypatch.setattr("marker_pdf_agent.worker.subprocess.run", fake_run)

    folder, error = ask_ollama_for_folder("missing", [], "# Document")

    assert folder is None
    assert error == "Ollama routing failed for model missing: model not found"


@pytest.mark.live_ollama
def test_live_ollama_reuses_existing_folder_for_related_documents() -> None:
    installed_models = list_ollama_models()
    model = next((name for name in ("llama3.1:latest", "llama3.1") if name in installed_models), None)
    if model is None:
        pytest.skip("Ollama with llama3.1 is not available")
    assert model is not None

    existing_folders = ["tufte-documentation", "invoices", "legal"]
    latex_folder, latex_error = ask_ollama_for_folder(
        model,
        existing_folders,
        "# Tufte LaTeX Documentation\n"
        "This document explains Tufte-style handouts, books, sidenotes, and margin figures.",
    )
    markdown_folder, markdown_error = ask_ollama_for_folder(
        model,
        existing_folders,
        "# Tufte R Markdown Styles\n"
        "This guide describes R Markdown formats for Tufte-style documents, margin notes, and handouts.",
    )

    assert latex_folder == "tufte-documentation"
    assert latex_error is None
    assert markdown_folder == "tufte-documentation"
    assert markdown_error is None


def test_build_config_defaults_to_launch_directory_without_ollama(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    args = Namespace(
        root=str(tmp_path),
        incoming="incoming",
        converted="converted",
        poll_interval=2.0,
        stable_seconds=1.0,
        marker_command="marker_single",
        marker_timeout=900.0,
        ollama_model=None,
        no_ollama=False,
    )

    config = build_config(args)

    assert config.root == tmp_path
    assert config.incoming_dir == tmp_path / "incoming"
    assert config.converted_dir == tmp_path / "converted"
    assert config.marker_timeout == 900.0
    assert config.ollama_model is None
    assert config.use_ollama is False


def test_build_config_uses_explicit_ollama_model(tmp_path: Path) -> None:
    args = Namespace(
        root=str(tmp_path),
        incoming="incoming",
        converted="converted",
        poll_interval=2.0,
        stable_seconds=1.0,
        marker_command="marker_single",
        marker_timeout=900.0,
        ollama_model="llama3.1",
        no_ollama=False,
    )

    config = build_config(args)

    assert config.ollama_model == "llama3.1"
    assert config.use_ollama is True


def test_list_ollama_models_parses_ollama_list(monkeypatch) -> None:
    output = (
        "NAME              ID              SIZE      MODIFIED\n"
        "llama3.1:latest   abc123          4.9 GB    today\n"
        "mistral:latest    def456          4.1 GB    today\n"
    )

    monkeypatch.setattr("marker_pdf_agent.worker.shutil.which", lambda command: "/usr/bin/ollama")
    monkeypatch.setattr(
        "marker_pdf_agent.worker.subprocess.run",
        lambda command, **kwargs: CompletedProcess(command, 0, stdout=output, stderr=""),
    )

    assert list_ollama_models() == ["llama3.1:latest", "mistral:latest"]


def test_parse_args_keeps_legacy_run_mode(tmp_path: Path) -> None:
    args = parse_args(["--root", str(tmp_path), "--no-ollama"])

    assert args.command == "run"
    assert args.root == str(tmp_path)
    assert args.no_ollama is True


def test_parse_args_supports_tray_modes(tmp_path: Path) -> None:
    direct = parse_args(["tray", "--root", str(tmp_path), "--no-ollama"])
    flagged = parse_args(["run", "--tray", "--root", str(tmp_path), "--no-ollama"])

    assert direct.command == "tray"
    assert direct.tray is True
    assert flagged.command == "run"
    assert flagged.tray is True


def test_save_and_load_monitored_roots_deduplicates(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    first = tmp_path / "first"
    second = tmp_path / "second"

    save_monitored_roots(config_path, [first, second, first])

    assert load_monitored_roots(config_path) == [first.resolve(), second.resolve()]
    assert '"roots"' in config_path.read_text(encoding="utf-8")


def test_save_and_load_agent_config_includes_ollama_model(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    root = tmp_path / "root"

    save_agent_config(config_path, [root], "llama3.1:latest")

    roots, ollama_model = load_agent_config(config_path)
    assert roots == [root.resolve()]
    assert ollama_model == "llama3.1:latest"


def test_build_tray_configs_persists_explicit_and_saved_roots(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    first = tmp_path / "first"
    second = tmp_path / "second"
    save_agent_config(config_path, [second], "llama3.1:latest")
    args = parse_args(["tray", "--root", str(first), "--config", str(config_path)])

    path, configs = build_tray_configs(args)

    assert path == config_path.resolve()
    assert [config.root for config in configs] == [first.resolve(), second.resolve()]
    assert load_monitored_roots(config_path) == [first.resolve(), second.resolve()]
    assert [config.ollama_model for config in configs] == ["llama3.1:latest", "llama3.1:latest"]


def test_run_tray_reports_missing_gui_dependency(monkeypatch, tmp_path: Path) -> None:
    args = parse_args(["tray", "--root", str(tmp_path), "--config", str(tmp_path / "config.json"), "--no-ollama"])

    def fake_run_tray_app(_manager: WorkerManager, _args: Namespace, _config_path: Path) -> None:
        raise RuntimeError('install GUI dependencies with: venv/bin/python -m pip install ".[gui]"')

    monkeypatch.setattr("marker_pdf_agent.tray.run_tray_app", fake_run_tray_app)

    with pytest.raises(RuntimeError, match="install GUI dependencies"):
        run_tray(args)


def test_service_run_arguments_use_current_python_and_run_subcommand(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("marker_pdf_agent.worker.sys.executable", "/example/python")
    args = parse_args(["install-service", "--root", str(tmp_path), "--incoming", "dropbox", "--no-ollama"])

    command = service_run_arguments(args)

    assert command[:5] == ["/example/python", "-m", "marker_pdf_agent.worker", "run", "--root"]
    assert str(tmp_path) in command
    assert "--incoming" in command
    assert "dropbox" in command
    assert command[-1] == "--no-ollama"


def test_install_launchd_service_writes_plist(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("marker_pdf_agent.worker.sys.executable", "/example/python")
    root = tmp_path / "managed"
    args = parse_args(["install-service", "--root", str(root), "--service-name", "marker-pdf-agent"])

    path = install_launchd_service(args)

    assert path == tmp_path / "home" / "Library" / "LaunchAgents" / "local.marker-pdf-agent.plist"
    text = path.read_text(encoding="utf-8")
    assert "local.marker-pdf-agent" in text
    assert str(root) in text
    assert "/example/python" in text


def test_install_systemd_user_service_writes_unit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("marker_pdf_agent.worker.sys.executable", "/example/python")
    root = tmp_path / "managed folder"
    args = parse_args(["install-service", "--root", str(root), "--marker-command", "marker single"])

    path = install_systemd_user_service(args)

    assert path == tmp_path / "home" / ".config" / "systemd" / "user" / "marker-pdf-agent.service"
    content = path.read_text(encoding="utf-8")
    assert "ExecStart=/example/python -m marker_pdf_agent.worker run --root" in content
    assert "'marker single'" in content
    assert "Restart=on-failure" in content


def test_install_windows_service_instructions_write_service_host_values(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("marker_pdf_agent.worker.sys.executable", "C:/Python/python.exe")
    root = tmp_path / "managed"
    args = parse_args(["install-service", "--root", str(root), "--service-name", "MarkerPdfAgent"])

    path = install_windows_service_instructions(args)

    content = path.read_text(encoding="utf-8")
    assert "Service name: MarkerPdfAgent" in content
    assert "Application: C:/Python/python.exe" in content
    assert "Arguments: -m marker_pdf_agent.worker run --root" in content


def test_service_label_adds_launchd_domain_when_needed() -> None:
    assert service_label("marker-pdf-agent") == "local.marker-pdf-agent"
    assert service_label("com.example.marker") == "com.example.marker"


def test_singleton_lock_rejects_second_running_agent(tmp_path: Path) -> None:
    lock_path = tmp_path / "agent.lock"

    with SingletonLock(lock_path), pytest.raises(RuntimeError, match="already running"), SingletonLock(lock_path):
        pass

    with SingletonLock(lock_path):
        assert lock_path.exists()


def test_singleton_lock_path_is_user_level(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert singleton_lock_path() == tmp_path / ".marker-pdf-agent" / "agent.lock"


def test_sanitize_folder_name_is_filesystem_friendly() -> None:
    assert sanitize_folder_name("  Tax & Legal / 2026!! ") == "tax-legal-2026"
    assert (
        sanitize_folder_name("tufte-latex-documentation/tufte-r-markdown-styles")
        == "tufte-latex-documentation-tufte-r-markdown-styles"
    )


def test_unique_path_adds_suffix_when_file_exists(tmp_path: Path) -> None:
    existing = tmp_path / "document.md"
    existing.write_text("old", encoding="utf-8")

    assert unique_path(existing) == tmp_path / "document-1.md"


class FakeProcess:
    def __init__(self, *, returncode_after_polls: int | None, final_returncode: int = 0) -> None:
        self.returncode_after_polls = returncode_after_polls
        self.final_returncode = final_returncode
        self.poll_count = 0
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        self.poll_count += 1
        if self.returncode_after_polls is not None and self.poll_count > self.returncode_after_polls:
            self.returncode = self.final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0
