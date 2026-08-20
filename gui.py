"""PySide6 GUI for the Presentation/PDF -> Markdown converter."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from converter import ConvertResult, SUPPORTED_EXTENSIONS, convert_files


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

    def __init__(self, paths: list[Path], output_dir: Path):
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

        self.file_list = DropList()
        self.file_list.setAlternatingRowColors(True)

        add_files_btn = QPushButton("Add Files...")
        add_folder_btn = QPushButton("Add Folder...")
        remove_btn = QPushButton("Remove")
        clear_btn = QPushButton("Clear")
        add_files_btn.clicked.connect(self.pick_files)
        add_folder_btn.clicked.connect(self.pick_folder)
        remove_btn.clicked.connect(self.remove_selected)
        clear_btn.clicked.connect(self.file_list.clear)

        file_buttons = QHBoxLayout()
        file_buttons.addWidget(add_files_btn)
        file_buttons.addWidget(add_folder_btn)
        file_buttons.addStretch()
        file_buttons.addWidget(remove_btn)
        file_buttons.addWidget(clear_btn)

        self.output_edit = QLineEdit(str(Path.home() / "Documents" / "Markdown"))
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.pick_output_dir)
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Output folder:"))
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse_btn)

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

        layout = QVBoxLayout()
        layout.addLayout(file_buttons)
        layout.addWidget(self.file_list, 1)
        layout.addLayout(output_row)
        layout.addWidget(self.convert_btn)
        layout.addWidget(self.progress)
        layout.addWidget(QLabel("Log:"))
        layout.addWidget(self.log, 1)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def closeEvent(self, event):
        if self._worker_thread is not None and self._worker_thread.isRunning():
            self._worker_thread.quit()
            self._worker_thread.wait()
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
            self, "Select files", "", "Presentations and PDFs (*.pptx *.pdf)"
        )
        if paths:
            self.add_paths([Path(p) for p in paths])

    def pick_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder")
        if folder:
            self.add_paths([Path(folder)])

    def pick_output_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder")
        if folder:
            self.output_edit.setText(folder)

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
        output_dir = Path(self.output_edit.text().strip() or ".")
        output_dir.mkdir(parents=True, exist_ok=True)

        self.convert_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setMaximum(len(paths))
        self.progress.setValue(0)
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
