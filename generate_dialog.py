from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)


class GenerationConfigDialog(QDialog):
    """Configuration popup for SANCHARI NED/INI generation."""

    def __init__(self, grandmasters, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Generate NED and INI")
        self.resize(520, 430)

        root = QVBoxLayout(self)

        general = QGroupBox("General")
        form = QFormLayout(general)

        self.package_edit = QLineEdit("sanchari")
        self.network_edit = QLineEdit("SANCHARI")
        self.config_edit = QLineEdit("General")
        self.description_edit = QLineEdit("Generated TSN scenario")
        self.sim_time_edit = QLineEdit("1s")

        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(["ps"])
        self.resolution_combo.setCurrentText("ps")

        form.addRow("NED package name", self.package_edit)
        form.addRow("Network name", self.network_edit)
        form.addRow("INI config name", self.config_edit)
        form.addRow("Description", self.description_edit)
        form.addRow("Simulation time limit", self.sim_time_edit)
        form.addRow("Time resolution", self.resolution_combo)
        root.addWidget(general)

        sync = QGroupBox("Time Synchronization")
        sync_form = QFormLayout(sync)

        self.sync_enabled = QCheckBox("Enable time synchronization")
        self.sync_enabled.setChecked(True)
        self.sync_enabled.toggled.connect(self._sync_controls_enabled)
        sync_form.addRow(self.sync_enabled)

        self.method_edit = QLineEdit("gPTP / IEEE 802.1AS")
        self.method_edit.setReadOnly(True)
        sync_form.addRow("Method", self.method_edit)

        self.grandmaster_combo = QComboBox()
        self.grandmaster_combo.addItems(grandmasters)
        sync_form.addRow("Grandmaster", self.grandmaster_combo)

        self.sync_interval_edit = QLineEdit("125ms")
        self.pdelay_interval_edit = QLineEdit("1s")
        sync_form.addRow("GM sync interval", self.sync_interval_edit)
        sync_form.addRow("Pdelay interval", self.pdelay_interval_edit)

        root.addWidget(sync)

        visualization = QGroupBox("Visualization")
        visualization_form = QFormLayout(visualization)
        self.multi_canvas_check = QCheckBox("Use multi-canvas visualizer")
        self.multi_canvas_check.setChecked(False)
        visualization_form.addRow(self.multi_canvas_check)
        root.addWidget(visualization)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._sync_controls_enabled(self.sync_enabled.isChecked())

    def _sync_controls_enabled(self, enabled: bool):
        self.method_edit.setEnabled(enabled)
        self.grandmaster_combo.setEnabled(enabled)
        self.sync_interval_edit.setEnabled(enabled)
        self.pdelay_interval_edit.setEnabled(enabled)

    def _accept(self):
        required = [
            (self.package_edit, "NED package name"),
            (self.network_edit, "Network name"),
            (self.config_edit, "INI config name"),
            (self.sim_time_edit, "Simulation time limit"),
        ]
        for widget, label in required:
            if not widget.text().strip():
                QMessageBox.warning(self, "Missing value", f"{label} cannot be empty.")
                widget.setFocus()
                return

        if self.sync_enabled.isChecked() and not self.grandmaster_combo.currentText():
            QMessageBox.warning(self, "Time Synchronization", "A Grandmaster is required when time synchronization is enabled.")
            return

        self.accept()

    def config(self) -> dict:
        return {
            "package": self.package_edit.text().strip(),
            "network_name": self.network_edit.text().strip(),
            "config_name": self.config_edit.text().strip(),
            "description": self.description_edit.text().strip(),
            "simulation_time": self.sim_time_edit.text().strip(),
            "time_resolution": self.resolution_combo.currentText(),
            "time_synchronization": {
                "enabled": self.sync_enabled.isChecked(),
                "method": self.method_edit.text().strip(),
                "grandmaster": self.grandmaster_combo.currentText(),
                "sync_interval": self.sync_interval_edit.text().strip(),
                "pdelay_interval": self.pdelay_interval_edit.text().strip(),
            },
            "visualization": {
                "multi_canvas": self.multi_canvas_check.isChecked(),
            },
        }
