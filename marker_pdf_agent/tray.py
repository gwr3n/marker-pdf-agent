from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import threading
from importlib import resources
from pathlib import Path

from marker_pdf_agent.worker import (
    WorkerManager,
    WorkerStatus,
    build_config_for_root,
    list_ollama_models,
    save_agent_config,
)


def run_tray_app(manager: WorkerManager, args: argparse.Namespace, config_path: Path) -> None:
    try:
        from PySide6.QtCore import QObject, Qt, Signal
        from PySide6.QtGui import QIcon, QPainter, QPixmap
        from PySide6.QtWidgets import (
            QApplication,
            QFileDialog,
            QMenu,
            QMessageBox,
            QSystemTrayIcon,
        )
    except ImportError as exc:
        raise RuntimeError("install GUI dependencies") from exc

    class StatusBridge(QObject):
        status_changed = Signal(object)
        models_changed = Signal(object)

    def make_icon() -> QIcon:
        icon_path = resources.files("marker_pdf_agent").joinpath("assets/file-markdown.svg")
        with resources.as_file(icon_path) as path:
            icon = QIcon(str(path))
        if not icon.isNull():
            return icon

        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.GlobalColor.black)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(5, 4, 20, 24, 3, 3)
        painter.setBrush(Qt.GlobalColor.white)
        painter.drawRect(9, 10, 12, 2)
        painter.drawRect(9, 15, 12, 2)
        painter.drawRect(9, 20, 8, 2)
        painter.end()
        return QIcon(pixmap)

    def open_path(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def quit_app() -> None:
        manager.stop_event.set()
        app.quit()

    def add_folder() -> None:
        folder = QFileDialog.getExistingDirectory(None, "Add monitored folder", str(Path.home()))
        if not folder:
            return
        root = Path(folder).resolve()
        config = build_config_for_root(args, root)
        if manager.add_config(config):
            save_current_config()
            refresh_menu()
        else:
            QMessageBox.information(None, "Already monitored", f"{root} is already monitored.")

    def remove_folder(root: Path) -> None:
        if manager.remove_root(root):
            save_current_config()
            refresh_menu()
        else:
            QMessageBox.information(None, "Cannot remove folder", "At least one folder must stay monitored.")

    available_ollama_models: list[str] = []
    selected_ollama_model = args.ollama_model

    def save_current_config() -> None:
        save_agent_config(config_path, manager.roots(), selected_ollama_model)

    def select_ollama_model(model: str | None) -> None:
        nonlocal selected_ollama_model
        selected_ollama_model = model
        args.ollama_model = model
        args.no_ollama = model is None
        manager.set_ollama_model(model)
        save_current_config()
        refresh_menu()

    def refresh_ollama_models() -> None:
        def load_models() -> None:
            bridge.models_changed.emit(list_ollama_models())

        threading.Thread(target=load_models, name="marker-ollama-models", daemon=True).start()

    def update_ollama_models(models: list[str]) -> None:
        nonlocal available_ollama_models
        available_ollama_models = models
        if selected_ollama_model and selected_ollama_model not in available_ollama_models:
            available_ollama_models = [selected_ollama_model, *available_ollama_models]
        refresh_menu()

    status_action = None
    queue_action = None

    def format_status(status: WorkerStatus) -> tuple[str, str]:
        if status.stopping:
            state = "Stopping"
        elif status.current_document:
            state = "Converting"
        else:
            state = "Idle"
        return state, f"Queue: {status.queue_size}"

    def update_status(status: WorkerStatus) -> None:
        if status_action is None or queue_action is None:
            return
        status_text, queue_text = format_status(status)
        status_action.setText(status_text)
        queue_action.setText(queue_text)

    def refresh_menu() -> None:
        nonlocal status_action, queue_action
        status = manager.status()
        status_text, queue_text = format_status(status)

        menu.clear()
        status_action = menu.addAction(status_text)
        status_action.setEnabled(False)
        queue_action = menu.addAction(queue_text)
        queue_action.setEnabled(False)
        menu.addSeparator()

        roots_menu = menu.addMenu("Monitored folders")
        for root_path in status.roots:
            root_menu = roots_menu.addMenu(str(root_path))
            root_menu.addAction("Open incoming", lambda checked=False, path=root_path: open_path(path / args.incoming))
            root_menu.addAction(
                "Open converted", lambda checked=False, path=root_path: open_path(path / args.converted)
            )
            root_menu.addAction("Remove", lambda checked=False, path=root_path: remove_folder(path))
        roots_menu.addSeparator()
        roots_menu.addAction("Add folder", add_folder)

        ollama_menu = menu.addMenu("Ollama routing")
        disabled_action = ollama_menu.addAction("Disabled")
        disabled_action.setCheckable(True)
        disabled_action.setChecked(selected_ollama_model is None)
        disabled_action.triggered.connect(lambda _checked=False: select_ollama_model(None))
        if available_ollama_models:
            ollama_menu.addSeparator()
            for model in available_ollama_models:
                model_action = ollama_menu.addAction(model)
                model_action.setCheckable(True)
                model_action.setChecked(model == selected_ollama_model)
                model_action.triggered.connect(lambda _checked=False, name=model: select_ollama_model(name))
        elif selected_ollama_model:
            selected_action = ollama_menu.addAction(selected_ollama_model)
            selected_action.setCheckable(True)
            selected_action.setChecked(True)
            selected_action.triggered.connect(lambda _checked=False: select_ollama_model(selected_ollama_model))
        ollama_menu.addSeparator()
        ollama_menu.addAction("Refresh models", refresh_ollama_models)
        menu.addSeparator()
        menu.addAction("Quit", quit_app)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    hide_macos_dock_icon()
    bridge = StatusBridge()
    bridge.status_changed.connect(update_status, Qt.ConnectionType.QueuedConnection)
    bridge.models_changed.connect(update_ollama_models, Qt.ConnectionType.QueuedConnection)

    icon = make_icon()
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("marker-pdf-agent")

    menu = QMenu()
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: (
            refresh_menu()
            if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.Context}
            else None
        )
    )
    refresh_menu()
    tray.show()
    manager.add_status_listener(lambda status: bridge.status_changed.emit(status))

    worker_thread = threading.Thread(target=manager.run, name="marker-tray-worker", daemon=True)
    worker_thread.start()

    exit_code = app.exec()
    manager.stop_event.set()
    worker_thread.join(timeout=10)
    if worker_thread.is_alive():
        raise RuntimeError("worker did not stop within 10 seconds")
    raise SystemExit(exit_code)


def hide_macos_dock_icon() -> None:
    if platform.system() != "Darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except ImportError:
        return
