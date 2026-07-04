from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import threading
from pathlib import Path

from marker_pdf_agent.worker import WorkerManager, WorkerStatus, build_config_for_root, save_monitored_roots


def run_tray_app(manager: WorkerManager, args: argparse.Namespace, config_path: Path) -> None:
    hide_macos_dock_icon()
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QIcon, QPainter, QPixmap
        from PySide6.QtWidgets import (
            QApplication,
            QFileDialog,
            QMenu,
            QMessageBox,
            QSystemTrayIcon,
        )
    except ImportError as exc:
        raise RuntimeError('install GUI dependencies with: venv/bin/python -m pip install ".[gui]"') from exc

    def make_icon() -> QIcon:
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
            save_monitored_roots(config_path, manager.roots())
            refresh_menu()
        else:
            QMessageBox.information(None, "Already monitored", f"{root} is already monitored.")

    def remove_folder(root: Path) -> None:
        if manager.remove_root(root):
            save_monitored_roots(config_path, manager.roots())
            refresh_menu()
        else:
            QMessageBox.information(None, "Cannot remove folder", "At least one folder must stay monitored.")

    status_action = None
    queue_action = None

    def format_status(status: WorkerStatus) -> tuple[str, str]:
        current = status.current_document or "Idle"
        root = f" in {status.current_root}" if status.current_root else ""
        state = "Stopping" if status.stopping else "Running"
        return f"{state}: {current}{root}", f"Queue: {status.queue_size}"

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
            root_menu.addAction("Open converted", lambda checked=False, path=root_path: open_path(path / args.converted))
            root_menu.addAction("Remove", lambda checked=False, path=root_path: remove_folder(path))
        roots_menu.addSeparator()
        roots_menu.addAction("Add folder", add_folder)
        menu.addSeparator()
        menu.addAction("Quit", quit_app)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    hide_macos_dock_icon()

    icon = make_icon()
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("marker-pdf-agent")

    menu = QMenu()
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: refresh_menu()
        if reason in {QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.Context}
        else None
    )
    refresh_menu()
    tray.show()
    manager.add_status_listener(update_status)

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
