"""PySide6 GUI for the Presentation/PDF -> Markdown converter."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from converter import ConvertResult, SUPPORTED_EXTENSIONS, config, convert_files
from converter.settings import get_setting, recent_files, record_recent, set_setting

_INPUT_DIR_KEY = "last_input_dir"
_OUTPUT_DIR_KEY = "last_output_dir"
_WINDOW_GEOMETRY_KEY = "window_geometry"
_PDF_MODE_KEY = "pdf_mode"
_AI_KEY_PREFIX = "ai_"

_TRUE_VALUES = {"1", "true", "yes", "on"}


class DropList(QListWidget):
    """File list accepting drag-and-dropped presentation/PDF files and folders."""

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        self.window().add_paths(paths)
        event.acceptProposedAction()


class ConversionThread(QThread):
    """Runs convert_files off the UI thread."""

    progressed = Signal(int, int, str)
    conversion_finished = Signal(list)

    def __init__(self, paths: list[Path], output_dir: Path | None):
        super().__init__()
        self._paths = paths
        self._output_dir = output_dir

    def run(self):
        results = convert_files(self._paths, self._output_dir, self._on_progress)
        self.conversion_finished.emit(results)

    def _on_progress(self, idx: int, total: int, name: str):
        self.progressed.emit(idx, total, name)


class HealthCheckThread(QThread):
    """Probes the local AI servers required by enabled features, off the UI thread."""

    checked = Signal(list)

    def run(self):
        results = []
        seen: set[str] = set()
        for key in config.enabled_keys():
            for name, url in config.feature_endpoints(key):
                if name in seen:
                    continue
                seen.add(name)
                results.append(
                    (name, config.probe(url), config.SERVERS[name].serve_command)
                )
        self.checked.emit(results)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Presentation to Markdown")
        self.resize(640, 520)
        self._worker_thread: QThread | None = None
        self._health_thread: QThread | None = None

        self._last_input_dir = get_setting(_INPUT_DIR_KEY) or ""
        self._last_output_dir = get_setting(_OUTPUT_DIR_KEY) or ""

        self.file_list = DropList()
        self.file_list.setAlternatingRowColors(True)

        add_files_btn = QPushButton("Add Files...")
        add_folder_btn = QPushButton("Add Folder...")
        self.recent_btn = QToolButton()
        self.recent_btn.setText("Recent")
        self.recent_btn.setPopupMode(QToolButton.InstantPopup)
        self.recent_menu = QMenu(self)
        self.recent_menu.aboutToShow.connect(self._refresh_recent_menu)
        self.recent_btn.setMenu(self.recent_menu)
        remove_btn = QPushButton("Remove")
        clear_btn = QPushButton("Clear")
        add_files_btn.clicked.connect(self.pick_files)
        add_folder_btn.clicked.connect(self.pick_folder)
        remove_btn.clicked.connect(self.remove_selected)
        clear_btn.clicked.connect(self.file_list.clear)

        file_buttons = QHBoxLayout()
        file_buttons.addWidget(add_files_btn)
        file_buttons.addWidget(add_folder_btn)
        file_buttons.addWidget(self.recent_btn)
        file_buttons.addStretch()
        file_buttons.addWidget(remove_btn)
        file_buttons.addWidget(clear_btn)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Defaults to <input-folder>/markdown")
        if self._last_output_dir:
            self.output_edit.setText(self._last_output_dir)
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.pick_output_dir)
        self.open_output_btn = QPushButton("Open")
        self.open_output_btn.clicked.connect(self.open_output_folder)
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Output folder:"))
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse_btn)
        output_row.addWidget(self.open_output_btn)

        self.paper_check = QCheckBox(
            "Paper layout (multi-column whitepapers — no per-page slide breaks)"
        )
        startup_mode = os.environ.get("PDF_MODE") or get_setting(_PDF_MODE_KEY, "slide")
        self.paper_check.setChecked(startup_mode == "paper")
        self.paper_check.toggled.connect(self._remember_pdf_mode)

        self.ai_checks: dict[str, QCheckBox] = {}
        ai_label = QLabel("AI features (need local model servers):")
        ai_checks_layout = QVBoxLayout()
        ai_checks_layout.addWidget(ai_label)
        self._load_ai_state()
        for key, feature in config.FEATURES.items():
            cb = QCheckBox(feature.label)
            cb.setToolTip(feature.description)
            cb.setChecked(config.is_enabled(key))
            cb.toggled.connect(lambda checked, k=key: self._toggle_feature(k, checked))
            self.ai_checks[key] = cb
            ai_checks_layout.addWidget(cb)
        self.status_label = QLabel()
        self.status_label.setTextFormat(Qt.RichText)
        self.status_label.setWordWrap(True)
        check_servers_btn = QPushButton("Check servers")
        check_servers_btn.clicked.connect(self._refresh_health)
        ai_status_row = QHBoxLayout()
        ai_status_row.addWidget(self.status_label, 1)
        ai_status_row.addWidget(check_servers_btn)

        self.convert_btn = QPushButton("Convert")
        self.convert_btn.clicked.connect(self.start_conversion)

        self.progress = QProgressBar()
        self.progress.setVisible(False)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText(
            "Drop .pptx/.pdf files or folders here, or use Add Files. "
            "One .md file per document, images saved under assets/<name>/."
        )

        copy_btn = QPushButton("Copy")
        copy_btn.clicked.connect(self.copy_log)
        clear_log_btn = QPushButton("Clear")
        clear_log_btn.clicked.connect(self.log.clear)
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("Log:"))
        log_header.addStretch()
        log_header.addWidget(copy_btn)
        log_header.addWidget(clear_log_btn)

        layout = QVBoxLayout()
        layout.addLayout(file_buttons)
        layout.addWidget(self.file_list, 1)
        layout.addLayout(output_row)
        layout.addLayout(ai_checks_layout)
        layout.addLayout(ai_status_row)
        layout.addWidget(self.paper_check)
        layout.addWidget(self.convert_btn)
        layout.addWidget(self.progress)
        layout.addLayout(log_header)
        layout.addWidget(self.log, 1)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self._restore_geometry()
        self._refresh_health()

    def _restore_geometry(self):
        geom = get_setting(_WINDOW_GEOMETRY_KEY)
        if not geom:
            return
        parts = geom.split(",")
        if len(parts) != 4:
            return
        try:
            x, y, w, h = (int(p) for p in parts)
        except ValueError:
            return
        self.move(x, y)
        self.resize(w, h)

    def closeEvent(self, event):
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait()
        set_setting(
            _WINDOW_GEOMETRY_KEY,
            f"{self.x()},{self.y()},{self.width()},{self.height()}",
        )
        super().closeEvent(event)

    def add_paths(self, paths: list[Path]):
        existing = {self.file_list.item(i).data(0) for i in range(self.file_list.count())}
        added = 0
        for path in paths:
            if path.is_dir():
                candidates = sorted(
                    cand
                    for cand in path.rglob("*")
                    if cand.suffix.lower() in SUPPORTED_EXTENSIONS
                )
            elif path.suffix.lower() in SUPPORTED_EXTENSIONS:
                candidates = [path]
            else:
                continue
            for cand in candidates:
                resolved = str(cand.resolve())
                if resolved not in existing:
                    item = QListWidgetItem(resolved)
                    item.setData(0, resolved)
                    self.file_list.addItem(item)
                    existing.add(resolved)
                    added += 1
        if added:
            self.log.appendPlainText(f"Added {added} file(s).")

    def pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select files",
            self._last_input_dir,
            "Presentations and PDFs (*.pptx *.pdf)",
        )
        if paths:
            self._remember_input_dir(Path(paths[0]).parent)
            self.add_paths([Path(p) for p in paths])

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select folder", self._last_input_dir
        )
        if folder:
            self._remember_input_dir(Path(folder))
            self.add_paths([Path(folder)])

    def pick_output_dir(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select output folder", self._last_output_dir
        )
        if folder:
            self.output_edit.setText(folder)
            self._last_output_dir = folder
            set_setting(_OUTPUT_DIR_KEY, folder)

    def _remember_input_dir(self, path: Path):
        self._last_input_dir = str(path)
        set_setting(_INPUT_DIR_KEY, self._last_input_dir)

    def _refresh_recent_menu(self):
        self.recent_menu.clear()
        entries = recent_files()
        if not entries:
            noop = QAction("No recent files", self.recent_menu)
            noop.setEnabled(False)
            self.recent_menu.addAction(noop)
            return
        for path in entries:
            action = QAction(path, self.recent_menu)
            action.triggered.connect(
                lambda checked=False, p=path: self.add_paths([Path(p)])
            )
            self.recent_menu.addAction(action)

    def copy_log(self):
        QApplication.clipboard().setText(self.log.toPlainText())

    def _resolve_output_dir(self) -> Path | None:
        text = self.output_edit.text().strip()
        if text:
            return Path(text)
        if self.file_list.count():
            return Path(self.file_list.item(0).data(0)).parent / "markdown"
        return None

    def open_output_folder(self):
        target = self._resolve_output_dir()
        if target is None:
            QMessageBox.information(
                self, "No output folder", "Add files or set an output folder first."
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def remove_selected(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))

    def _remember_pdf_mode(self, checked: bool):
        set_setting(_PDF_MODE_KEY, "paper" if checked else "slide")

    def _load_ai_state(self):
        """Seed runtime AI state from stored settings for env vars left unset.

        The environment (from ``ptm-start --vision`` etc.) takes precedence: a
        feature whose env var is set keeps the value ``config`` read at import.
        """
        for key, feature in config.FEATURES.items():
            if os.environ.get(feature.env_var) is not None:
                continue
            stored = get_setting(_AI_KEY_PREFIX + key)
            if stored is not None:
                config.set_enabled(key, stored.strip().lower() in _TRUE_VALUES)

    def _persist_ai_state(self):
        for key in config.FEATURES:
            set_setting(_AI_KEY_PREFIX + key, "1" if config.is_enabled(key) else "0")

    def _sync_ai_checkboxes(self):
        for key, checkbox in self.ai_checks.items():
            checkbox.blockSignals(True)
            checkbox.setChecked(config.is_enabled(key))
            checkbox.blockSignals(False)

    def _toggle_feature(self, key: str, checked: bool):
        config.set_enabled(key, checked)
        self._persist_ai_state()
        self._sync_ai_checkboxes()
        self._refresh_health()

    def _refresh_health(self):
        if self._health_thread is not None and self._health_thread.isRunning():
            return
        self._health_thread = HealthCheckThread()
        self._health_thread.checked.connect(self._on_health)
        self._health_thread.finished.connect(self._health_thread.deleteLater)
        self._health_thread.start()

    def _on_health(self, results):
        if not config.enabled_keys():
            self.status_label.setText("AI features are disabled.")
            return
        lines = ["<b>AI servers:</b>"]
        for name, up, command in results:
            state = "up" if up else "down"
            color = "#2e7d32" if up else "#c62828"
            extra = "" if up else f" — run: <code>{command}</code>"
            lines.append(f'<span style="color:{color}">{name}</span> ({state}){extra}')
        self.status_label.setText("<br>".join(lines))

    def _block_on_servers(self, missing) -> bool:
        """Modal "block until up" gate: re-probe until the servers answer or the
        user cancels. Returns True when conversion may proceed."""
        while True:
            lines = [f"• {name} — <code>{command}</code>" for name, _, command in missing]
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("AI server not running")
            box.setText(
                "Some enabled AI features need a local model server that is not "
                "running. Start it in a terminal, then click Retry:"
            )
            box.setInformativeText("\n".join(lines))
            box.setDetailedText("\n".join(name for name, _, _ in missing))
            retry = box.addButton("Retry", QMessageBox.AcceptRole)
            cancel = box.addButton("Cancel", QMessageBox.RejectRole)
            box.setDefaultButton(retry)
            box.exec()
            if box.clickedButton() is cancel:
                return False
            missing = config.missing_servers()
            if not missing:
                return True

    def _ai_preflight(self) -> bool:
        """Block conversion until every enabled feature's server is up."""
        missing = config.missing_servers()
        if not missing:
            return True
        return self._block_on_servers(missing)

    def start_conversion(self):
        os.environ["PDF_MODE"] = "paper" if self.paper_check.isChecked() else "slide"
        self._remember_pdf_mode(self.paper_check.isChecked())
        paths = [
            Path(self.file_list.item(i).data(0))
            for i in range(self.file_list.count())
        ]
        if not paths:
            QMessageBox.warning(self, "No files", "Add at least one .pptx or .pdf file first.")
            return

        if not self._ai_preflight():
            return

        text = self.output_edit.text().strip()
        output_dir = Path(text) if text else None
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
        else:
            parents = {path.parent for path in paths}
            if len(parents) > 1:
                QMessageBox.warning(
                    self,
                    "Files from different folders",
                    "Selected files come from different folders. Each file will be "
                    "written to its own <folder>/markdown/ subfolder.",
                )

        self.convert_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setMaximum(len(paths))
        self.progress.setValue(0)
        if output_dir is None:
            self.log.appendPlainText(
                f"Converting {len(paths)} file(s) to <input-folder>/markdown ..."
            )
        else:
            self.log.appendPlainText(f"Converting {len(paths)} file(s) to {output_dir} ...")

        self._worker_thread = ConversionThread(paths, output_dir)
        self._worker_thread.progressed.connect(self._on_progress)
        self._worker_thread.conversion_finished.connect(self._on_finished)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    def _on_progress(self, idx: int, total: int, name: str):
        self.progress.setValue(idx)
        self.log.appendPlainText(f"[{idx}/{total}] {name}")

    def _on_finished(self, results: list[ConvertResult]):
        ok = 0
        for result in results:
            record_recent(str(result.source_path.resolve()))
            if result.error:
                self.log.appendPlainText(f"[ERR] {result.source_path.name}: {result.error}")
            else:
                ok += 1
                self.log.appendPlainText(f"[OK]  {result.source_path.name} -> {result.md_path}")
                for warning in result.warnings:
                    self.log.appendPlainText(f"[WARN] {result.source_path.name}: {warning}")
        self.log.appendPlainText(f"Done: {ok} of {len(results)} converted.\n")
        self.progress.setVisible(False)
        self.convert_btn.setEnabled(True)
        self._worker_thread = None


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
