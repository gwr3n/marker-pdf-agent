from argparse import Namespace
from pathlib import Path

from marker_pdf_agent.worker import (
    MarkerPdfWorker,
    WorkerConfig,
    build_config,
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


def test_unique_path_adds_suffix_when_file_exists(tmp_path: Path) -> None:
    existing = tmp_path / "document.md"
    existing.write_text("old", encoding="utf-8")

    assert unique_path(existing) == tmp_path / "document-1.md"
