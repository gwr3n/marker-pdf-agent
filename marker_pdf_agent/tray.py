from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import threading
from pathlib import Path

from marker_pdf_agent.worker import WorkerManager, build_config_for_root, save_monitored_roots


def run_tray_app(manager: WorkerManager, args: argparse.Namespace, config_path: Path) -> None:
    try:
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QAction, QIcon, QPainter, QPixmap
        from PySide6.QtWidgets import (
            QApplication,
            QFileDialog,
            QHBoxLayout,
            QLabel,
            QListWidget,
            QMainWindow,
            QMenu,
            QMessageBox,
            QPushButton,
            QSystemTrayIcon,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        raise RuntimeError('install GUI dependencies with: venv/bin/python -m pip install ".[gui]"') from exc

    class StatusWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("marker-pdf-agent")
            self.resize(520, 360)

            self.summary_label = QLabel()
            self.summary_label.setWordWrap(True)
            self.roots_list = QListWidget()
            self.open_incoming_button = QPushButton("Open incoming")
            self.open_converted_button = QPushButton("Open converted")
            self.add_button = QPushButton("Add folder")
            self.remove_button = QPushButton("Remove folder")
            self.quit_button = QPushButton("Quit")

            button_row = QHBoxLayout()
            button_row.addWidget(self.open_incoming_button)
            button_row.addWidget(self.open_converted_button)

            manage_row = QHBoxLayout()
            manage_row.addWidget(self.add_button)
            manage_row.addWidget(self.remove_button)
            manage_row.addStretch(1)
            manage_row.addWidget(self.quit_button)

            layout = QVBoxLayout()
            layout.addWidget(self.summary_label)
            layout.addWidget(QLabel("Monitored folders"))
            layout.addWidget(self.roots_list)
            layout.addLayout(button_row)
            layout.addLayout(manage_row)

            container = QWidget()
            container.setLayout(layout)
            self.setCentralWidget(container)

            self.open_incoming_button.clicked.connect(lambda: self.open_selected("incoming"))
            self.open_converted_button.clicked.connect(lambda: self.open_selected("converted"))
            self.add_button.clicked.connect(self.add_folder)
            self.remove_button.clicked.connect(self.remove_folder)
            self.quit_button.clicked.connect(quit_app)

        def refresh(self) -> None:
            status = manager.status()
            current = status.current_document or "Idle"
            root = f" in {status.current_root}" if status.current_root else ""
            stopping = "Stopping" if status.stopping else "Running"
            self.summary_label.setText(f"{stopping}. Current: {current}{root}. Queue: {status.queue_size}.")

            selected = self.selected_root()
            self.roots_list.clear()
            for root_path in status.roots:
                self.roots_list.addItem(str(root_path))
            if selected:
                matches = self.roots_list.findItems(str(selected), Qt.MatchFlag.MatchExactly)
                if matches:
                    self.roots_list.setCurrentItem(matches[0])
            elif self.roots_list.count():
                self.roots_list.setCurrentRow(0)

        def selected_root(self) -> Path | None:
            item = self.roots_list.currentItem()
            return Path(item.text()) if item else None

        def open_selected(self, child: str) -> None:
            root = self.selected_root()
            if root is None:
                return
            open_path(root / child)

        def add_folder(self) -> None:
            folder = QFileDialog.getExistingDirectory(self, "Add monitored folder", str(Path.home()))
            if not folder:
                return
            root = Path(folder).resolve()
            config = build_config_for_root(args, root)
            if manager.add_config(config):
                save_monitored_roots(config_path, manager.roots())
                self.refresh()
            else:
                QMessageBox.information(self, "Already monitored", f"{root} is already monitored.")

        def remove_folder(self) -> None:
            root = self.selected_root()
            if root is None:
                return
            if manager.remove_root(root):
                save_monitored_roots(config_path, manager.roots())
                self.refresh()
            else:
                QMessageBox.information(self, "Cannot remove folder", "At least one folder must stay monitored.")

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

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)

    icon = make_icon()
    window = StatusWindow()
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("marker-pdf-agent")

    menu = QMenu()
    show_action = QAction("Show status")
    quit_action = QAction("Quit")
    show_action.triggered.connect(lambda: show_window())
    quit_action.triggered.connect(quit_app)
    menu.addAction(show_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.show()

    def show_window() -> None:
        window.refresh()
        window.show()
        window.raise_()
        window.activateWindow()

    worker_thread = threading.Thread(target=manager.run, name="marker-tray-worker", daemon=True)
    worker_thread.start()

    timer = QTimer()
    timer.timeout.connect(window.refresh)
    timer.start(1000)
    window.refresh()

    exit_code = app.exec()
    manager.stop_event.set()
    worker_thread.join(timeout=10)
    if worker_thread.is_alive():
        raise RuntimeError("worker did not stop within 10 seconds")
    raise SystemExit(exit_code)
