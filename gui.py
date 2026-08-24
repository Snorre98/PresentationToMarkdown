"""PySide6 GUI for the Presentation/PDF -> Markdown converter."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
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

from converter import ConvertResult, SUPPORTED_EXTENSIONS, convert_files
from converter.settings import get_setting, recent_files, record_recent, set_setting

_INPUT_DIR_KEY = "last_input_dir"
_OUTPUT_DIR_KEY = "last_output_dir"
_WINDOW_GEOMETRY_KEY = "window_geometry"


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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Presentation to Markdown")
        self.resize(640, 520)
        self._worker_thread: QThread | None = None

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
        layout.addWidget(self.convert_btn)
        layout.addWidget(self.progress)
        layout.addLayout(log_header)
        layout.addWidget(self.log, 1)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self._restore_geometry()

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

    def start_conversion(self):
        paths = [
            Path(self.file_list.item(i).data(0))
            for i in range(self.file_list.count())
        ]
        if not paths:
            QMessageBox.warning(self, "No files", "Add at least one .pptx or .pdf file first.")
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
