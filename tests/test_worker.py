from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from marker_pdf_agent.worker import (
    MarkerPdfWorker,
    WorkerConfig,
    ask_ollama_for_folder,
    build_config,
    discover_ollama_model,
    sanitize_folder_name,
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

    def fake_ollama(model: str, existing_folders: list[str], markdown_text: str) -> str:
        assert model == "llama3.1"
        assert existing_folders == ["finance"]
        assert "Invoice" in markdown_text
        return "Finance"

    monkeypatch.setattr("marker_pdf_agent.worker.ask_ollama_for_folder", fake_ollama)
    worker = MarkerPdfWorker(config)

    assert worker._choose_destination(markdown_file) == tmp_path / "converted" / "finance"


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

    def fake_ollama(_model: str, existing_folders: list[str], markdown_text: str) -> str:
        if "Tufte" in markdown_text:
            return "tufte-documentation"
        if "Invoice" in markdown_text:
            return "invoices"
        if "Lease" in markdown_text:
            return "legal"
        if "Resume" in markdown_text:
            return "career"
        return "uncategorized"

    monkeypatch.setattr(worker, "_run_marker", fake_run_marker)
    monkeypatch.setattr("marker_pdf_agent.worker.ask_ollama_for_folder", fake_ollama)

    for source in sorted(config.incoming_dir.iterdir()):
        worker._process_document(source)

    actual_tree = sorted(
        str(path.relative_to(config.converted_dir))
        for path in config.converted_dir.rglob("*")
        if path.is_file()
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
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return CompletedProcess(command, 0, stdout="tax-records\n", stderr="")

    monkeypatch.setattr("marker_pdf_agent.worker.subprocess.run", fake_run)

    folder = ask_ollama_for_folder("llama3.1", ["finance", "legal"], "# Tax return\nW-2 income")

    assert folder == "tax-records"
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

    folder = ask_ollama_for_folder(
        "llama3.1",
        ["tufte-latex-documentation"],
        "# Tufte R Markdown Styles\nDocumentation for Tufte-style documents",
    )

    assert folder == "tufte-latex-documentation"


@pytest.mark.live_ollama
def test_live_ollama_reuses_existing_folder_for_related_documents() -> None:
    model = discover_ollama_model("llama3.1")
    if model is None:
        pytest.skip("Ollama with llama3.1 is not available")

    existing_folders = ["tufte-documentation", "invoices", "legal"]
    latex_folder = ask_ollama_for_folder(
        model,
        existing_folders,
        "# Tufte LaTeX Documentation\nThis document explains Tufte-style handouts, books, sidenotes, and margin figures.",
    )
    markdown_folder = ask_ollama_for_folder(
        model,
        existing_folders,
        "# Tufte R Markdown Styles\nThis guide describes R Markdown formats for Tufte-style documents, margin notes, and handouts.",
    )

    assert latex_folder == "tufte-documentation"
    assert markdown_folder == "tufte-documentation"


def test_build_config_defaults_to_launch_directory_and_detected_ollama(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("marker_pdf_agent.worker.discover_ollama_model", lambda preferred: "llama3.1")
    args = Namespace(
        root=str(tmp_path),
        incoming="incoming",
        converted="converted",
        poll_interval=2.0,
        stable_seconds=1.0,
        marker_command="marker_single",
        ollama_model=None,
        no_ollama=False,
    )

    config = build_config(args)

    assert config.root == tmp_path
    assert config.incoming_dir == tmp_path / "incoming"
    assert config.converted_dir == tmp_path / "converted"
    assert config.ollama_model == "llama3.1"


def test_sanitize_folder_name_is_filesystem_friendly() -> None:
    assert sanitize_folder_name("  Tax & Legal / 2026!! ") == "tax-legal-2026"
    assert sanitize_folder_name("tufte-latex-documentation/tufte-r-markdown-styles") == "tufte-latex-documentation-tufte-r-markdown-styles"


def test_unique_path_adds_suffix_when_file_exists(tmp_path: Path) -> None:
    existing = tmp_path / "document.md"
    existing.write_text("old", encoding="utf-8")

    assert unique_path(existing) == tmp_path / "document-1.md"
