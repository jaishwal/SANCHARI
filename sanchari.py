#!/usr/bin/env python3
"""
pip install PySide6
"""

from __future__ import annotations

import json
import math
import sys

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from generate_dialog import GenerationConfigDialog
import generator


# =============================================================
# Simple x/y container passed into the click / drag / release
# handlers below, so those methods don't need to know anything
# about Qt's own mouse-event objects.
# =============================================================

class _Evt:
    __slots__ = ("x", "y")

    def __init__(self, x, y):
        self.x = x
        self.y = y


# =============================================================
# Canvas widget: owns nothing but painting and mouse capture.
# All state and logic live on the TSNDesigner (the "app").
# =============================================================

class NetworkCanvas(QWidget):

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setMinimumSize(400, 300)
        self.setStyleSheet("background-color: white; border: 1px solid #999999;")
        self.setMouseTracking(False)  # move events only fire while a button is held down

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self.app.paint_network(painter, self.width(), self.height())
        painter.end()

    def mousePressEvent(self, event):
        pos = event.position()
        # self.app.canvas_click(_Evt(pos.x(), pos.y()))
        # Check mouse press events for dragging device
        if event.button() == Qt.LeftButton:
            self.app.begin_mouse_press(_Evt(pos.x(), pos.y()))

    def mouseMoveEvent(self, event):
        pos = event.position()

        if event.buttons() & Qt.LeftButton:
            self.app.drag_device(_Evt(pos.x(), pos.y()))

    def mouseReleaseEvent(self, event):
        pos = event.position()

        if event.button() == Qt.LeftButton:
            self.app.release_device(_Evt(pos.x(), pos.y()))


# =============================================================
# Main application window
# =============================================================

class TSNDesigner(QMainWindow):

    # ---- flow presets, used throughout the Add/Edit Flow dialogs ----

    FLOW_ENTRY_FIELDS = [
        ("payload_size_kb", "Payload Size (KB):", "100"),
        ("deadline_microsec", "Deadline (\u00b5s):", "10"),
        ("stream_periodicity_microsec", "Stream Periodicity (\u00b5s):", "20"),
        ("burst_size_mb", "Burst Size (MB):", "15"),
        ("max_jitter_microsec", "Max Jitter (\u00b5s):", "5"),
        ("bandwidth_mbps", "Bandwidth (Mbps):", "50"),
    ]

    FLOW_TRAFFIC_TYPES = ["HRT", "AVB-A", "AVB-B", "BE"]
    FLOW_PCP_QUEUES = [str(i) for i in range(8)]
    FLOW_RELIABILITY_OPTIONS = ["0.9", "0.95", "0.99", "0.999", "0.9999"]

    FLOW_COLOR_PALETTE = [
        "#e6194b", "#4363d8", "#911eb4", "#f032e6", "#469990",
        "#9a6324", "#000075", "#808000", "#3cb44b", "#f58231",
    ]

    FLOW_DASH_BY_TRAFFIC = {
        "HRT": None,
        "AVB-A": (6, 2),
        "AVB-B": (3, 2),
        "BE": (1, 3),
    }

    FLOW_WIDTH_BY_TRAFFIC = {
        "HRT": 3,
        "AVB-A": 2,
        "AVB-B": 2,
        "BE": 1,
    }

    def __init__(self):
        super().__init__()

        self.setWindowTitle("SANCHARI")
        self.resize(1200, 800)

        # -------------------------------------------------
        # Core topology state
        # -------------------------------------------------

        self.area_width = 100
        self.area_height = 80
        self.grid_size = 5

        self.devices = {}
        self.links = []

        self.es_count = 0
        self.sw_count = 0
        self.gm_count = 0
        self.link_count = 0

        self.flows = []
        self.flow_count = 0

        self.show_flows = True

        self.mode = "select"

        self.selected_device = None
        self.link_start = None

        self.dragging_device =  None

        self.device_radius = 12

        # Defaults until the first paint fills in the real values
        # (physical_to_canvas / canvas_to_physical rely on these).
        self.scale = 1.0
        self.scale_x = 1.0
        self.scale_y = 1.0
        self.origin_x = 40
        self.origin_y = 40

        # Non-modal dialogs (.show(), not .exec()) need a live reference
        # or Python garbage-collects them immediately after this method
        # returns and the window silently never appears.
        self._open_dialogs = []

        # -------------------------------------------------
        # Central widget / root layout
        # -------------------------------------------------

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(5, 5, 5, 5)

        # -------------------------------------------------
        # Top control panel
        # -------------------------------------------------

        control = QWidget()
        control_layout = QHBoxLayout(control)
        control_layout.setContentsMargins(0, 0, 0, 5)
        root_layout.addWidget(control)

        control_layout.addWidget(QLabel("Area Length (m):"))
        self.width_entry = QLineEdit("100")
        self.width_entry.setFixedWidth(60)
        control_layout.addWidget(self.width_entry)

        control_layout.addWidget(QLabel("Area Breadth (m):"))
        self.height_entry = QLineEdit("80")
        self.height_entry.setFixedWidth(60)
        control_layout.addWidget(self.height_entry)

        control_layout.addWidget(QLabel("Grid (m):"))
        self.grid_entry = QLineEdit("5")
        self.grid_entry.setFixedWidth(45)
        control_layout.addWidget(self.grid_entry)

        btn_grid = QPushButton("Create Grid")
        btn_grid.clicked.connect(self.create_grid)
        control_layout.addWidget(btn_grid)

        # ---- modes ----

        # Each entry is (label shown in the dropdown, placement mode it
        # activates). The label deliberately names the real INET/NED
        # class in parentheses, since that's what this device will
        # eventually be generated as.
        self.ADD_COMPONENT_OPTIONS = [
            ("End System (TsnDevice)", "place_es"),
            ("Switch (TsnSwitch)", "place_switch"),
            ("GrandMaster (TsnClock)", "place_grandmaster"),
        ]

        control_layout.addWidget(QLabel("Add Components:"))
        self.add_component_combo = QComboBox()
        self.add_component_combo.addItems(
            [label for label, _ in self.ADD_COMPONENT_OPTIONS]
        )
        # .activated (not .currentIndexChanged) so this only fires when the
        # user actually picks something, and fires again even if the same
        # item is picked twice in a row.
        self.add_component_combo.activated.connect(self._on_add_component_selected)
        control_layout.addWidget(self.add_component_combo)

        btn_link = QPushButton("Create Link")
        btn_link.clicked.connect(lambda: self.set_mode("link"))
        control_layout.addWidget(btn_link)

        btn_select = QPushButton("Select")
        btn_select.clicked.connect(lambda: self.set_mode("select"))
        control_layout.addWidget(btn_select)

        # btn_delete = QPushButton("Delete")
        # btn_delete.clicked.connect(lambda: self.set_mode("delete"))
        # control_layout.addWidget(btn_delete)

        btn_add_flow = QPushButton("Add Flow")
        btn_add_flow.clicked.connect(self.add_flow)
        control_layout.addWidget(btn_add_flow)

        self.show_flows_checkbox = QCheckBox("Show Flows")
        self.show_flows_checkbox.setChecked(True)
        self.show_flows_checkbox.toggled.connect(self._on_show_flows_toggled)
        control_layout.addWidget(self.show_flows_checkbox)

        btn_save = QPushButton("Save")
        btn_save.clicked.connect(self.save_topology)
        control_layout.addWidget(btn_save)

        btn_generate = QPushButton("Generate NED and INI")
        btn_generate.clicked.connect(self.generate_ned_ini)
        control_layout.addWidget(btn_generate)

        control_layout.addStretch(1)

        # -------------------------------------------------
        # Main area: canvas (left) + info panel (right)
        # -------------------------------------------------

        main = QWidget()
        main_layout = QHBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(main, 1)

        self.canvas = NetworkCanvas(self)
        main_layout.addWidget(self.canvas, 1)

        # ---- information panel ----

        info = QWidget()
        info.setFixedWidth(250)
        info_layout = QVBoxLayout(info)
        main_layout.addWidget(info)

        title = QLabel("Device Information")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        title.setAlignment(Qt.AlignCenter)
        info_layout.addWidget(title)

        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        info_layout.addWidget(self.info_text, 1)

        btn_edit_device = QPushButton("Edit Selected Device")
        btn_edit_device.clicked.connect(self.edit_device)
        info_layout.addWidget(btn_edit_device)

        btn_edit_link = QPushButton("Edit Selected Link")
        btn_edit_link.clicked.connect(self.edit_link)
        info_layout.addWidget(btn_edit_link)

        btn_redundant = QPushButton("View Redundant Links")
        btn_redundant.clicked.connect(self.show_redundant_links)
        info_layout.addWidget(btn_redundant)

        btn_manage_flows = QPushButton("Manage Flows")
        btn_manage_flows.clicked.connect(lambda: self.edit_flow())
        info_layout.addWidget(btn_manage_flows)

        btn_delete_selected = QPushButton("Delete Selected Device")
        btn_delete_selected.clicked.connect(self.delete_selected)
        info_layout.addWidget(btn_delete_selected)

        flows_label = QLabel("Flows")
        flows_label.setStyleSheet("font-weight: bold;")
        info_layout.addWidget(flows_label)

        self.flow_listbox = QListWidget()
        self.flow_listbox.setFixedHeight(140)
        self.flow_listbox.itemDoubleClicked.connect(self.on_flow_listbox_double_click)
        info_layout.addWidget(self.flow_listbox)

        # btn_delete_selected = QPushButton("Delete Selected")
        # btn_delete_selected.clicked.connect(self.delete_selected)
        # info_layout.addWidget(btn_delete_selected)

        # -------------------------------------------------
        # Status bar
        # -------------------------------------------------

        self.status = QLabel("Mode: Select")
        self.status.setFrameShape(QFrame.Panel)
        self.status.setFrameShadow(QFrame.Sunken)
        self.status.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        root_layout.addWidget(self.status)

        self._build_menu()

        self.create_grid()

    def _build_menu(self) -> None:

        menu = self.menuBar()
        file_menu = menu.addMenu("&File")

        act_new = QAction("&New", self)
        act_new.setShortcut(QKeySequence.New)
        act_new.triggered.connect(self.new_project)
        file_menu.addAction(act_new)

        act_example = QAction("New from &Example", self)
        act_example.triggered.connect(self.new_from_example)
        file_menu.addAction(act_example)

        file_menu.addSeparator()

        act_open = QAction("&Open Project...", self)
        act_open.setShortcut(QKeySequence.Open)
        act_open.triggered.connect(self.open_project)
        file_menu.addAction(act_open)

        act_save = QAction("&Save Project...", self)
        act_save.setShortcut(QKeySequence.Save)
        act_save.triggered.connect(self.save_project)
        file_menu.addAction(act_save)

        file_menu.addSeparator()

        act_export = QAction("&Export NED and ini...", self)
        act_export.triggered.connect(self.export_ned_ini)
        file_menu.addAction(act_export)

        file_menu.addSeparator()

        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

    def _on_show_flows_toggled(self, checked: bool) -> None:
        self.show_flows = checked
        self.redraw_network()

    def _on_add_component_selected(self, index: int) -> None:
        _, mode = self.ADD_COMPONENT_OPTIONS[index]
        self.set_mode(mode)

    # =====================================================
    # GRID
    # =====================================================

    def create_grid(self):

        try:
            self.area_width = float(self.width_entry.text())
            self.area_height = float(self.height_entry.text())
            self.grid_size = float(self.grid_entry.text())

            if self.area_width <= 0 or self.area_height <= 0 or self.grid_size <= 0:
                raise ValueError

        except ValueError:
            QMessageBox.critical(self, "Error", "Enter valid numeric values.")
            return

        # Repainting the whole canvas (grid + everything else) is
        # handled by redraw_network() -> canvas.update() -> paintEvent.
        self.redraw_network()

    # =====================================================
    # DRAW GRID
    # =====================================================

    def format_grid_value(self, value):
        """Format grid coordinates correctly, including fractional steps."""

        value = round(float(value), 10)

        if abs(value) < 1e-10:
            value = 0.0

        if value.is_integer():
            return str(int(value))

        return f"{value:.10f}".rstrip("0").rstrip(".")

    def _compute_grid_geometry(self, canvas_width, canvas_height):
        if canvas_width < 100:
            canvas_width = 850
        if canvas_height < 100:
            canvas_height = 650

        margin = 40

        self.scale_x = (canvas_width - 2 * margin) / self.area_width
        self.scale_y = (canvas_height - 2 * margin) / self.area_height
        self.scale = min(self.scale_x, self.scale_y)

        self.origin_x = margin
        self.origin_y = margin

    def draw_grid(self, painter: QPainter):

        pen = QPen(QColor("#dddddd"))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setFont(QFont("Arial", 8))

        # Vertical grid
        x = 0.0
        while x <= self.area_width:
            px = self.origin_x + x * self.scale
            painter.setPen(pen)
            painter.drawLine(
                QPointF(px, self.origin_y),
                QPointF(px, self.origin_y + self.area_height * self.scale),
            )
            self._draw_centered_text(
                painter, px, self.origin_y - 15, self.format_grid_value(x), QColor("black")
            )
            x += self.grid_size

        # Horizontal grid
        y = 0.0
        while y <= self.area_height:
            py = self.origin_y + y * self.scale
            painter.setPen(pen)
            painter.drawLine(
                QPointF(self.origin_x, py),
                QPointF(self.origin_x + self.area_width * self.scale, py),
            )
            self._draw_centered_text(
                painter, self.origin_x - 20, py, self.format_grid_value(y), QColor("black")
            )
            y += self.grid_size

    # =====================================================
    # DRAWING PRIMITIVES
    # =====================================================

    def _draw_centered_text(self, painter: QPainter, x, y, text, color, font=None):
        """Draws text centered on (x, y) rather than anchored at a corner."""

        if font is not None:
            painter.setFont(font)
        metrics = painter.fontMetrics()
        rect = metrics.boundingRect(text)
        pen = QPen(QColor(color))
        painter.setPen(pen)
        painter.drawText(
            QPointF(x - rect.width() / 2, y + metrics.ascent() / 2 - metrics.descent() / 2),
            text,
        )

    def _set_dashed_pen(self, pen: QPen, dash):
        """Applies a dash pattern given in plain pixel on/off lengths.

        Qt expresses dash patterns as multiples of the pen width, so the
        requested pixel lengths are divided by the pen width first --
        this keeps the dash/gap size visually consistent no matter how
        thick the line is.
        """

        if dash:
            w = max(pen.widthF(), 1.0)
            pen.setDashPattern([d / w for d in dash])

    def _draw_polyline(self, painter: QPainter, points, color, width, dash=None):
        pen = QPen(QColor(color))
        pen.setWidthF(width)
        self._set_dashed_pen(pen, dash)
        painter.setPen(pen)
        poly = QPolygonF([QPointF(points[i], points[i + 1]) for i in range(0, len(points), 2)])
        painter.drawPolyline(poly)

    def _draw_arrowhead(self, painter: QPainter, x1, y1, x2, y2, color):
        """Small filled triangle at (x2, y2), oriented along the (x1,y1)->(x2,y2) direction."""

        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy) or 1
        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux

        tip = QPointF(x2, y2)
        back_x = x2 - ux * 12
        back_y = y2 - uy * 12
        left = QPointF(back_x + nx * 4, back_y + ny * 4)
        right = QPointF(back_x - nx * 4, back_y - ny * 4)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawPolygon(QPolygonF([tip, left, right]))
        painter.setBrush(Qt.NoBrush)

    def _draw_device_icon(self, painter: QPainter, device_type, x, y):
        """Draws the small icon that represents one device on the canvas,
        chosen by device type: a monitor for an end system, a switch body
        with ports for a switch, and a clock face for a grandmaster."""

        pen = QPen(QColor("black"))
        pen.setWidth(2)
        painter.setPen(pen)

        if device_type == "switch":

            painter.setBrush(QColor("orange"))
            painter.drawRect(QRectF(x - 14, y - 10, 28, 20))
            painter.setBrush(Qt.NoBrush)

            # Port ticks along the bottom edge
            painter.setBrush(QColor("black"))
            port_count = 4
            port_w, port_h, gap = 3, 5, 3
            total_w = port_count * port_w + (port_count - 1) * gap
            start_x = x - total_w / 2
            for i in range(port_count):
                px = start_x + i * (port_w + gap)
                painter.drawRect(QRectF(px, y + 9, port_w, port_h))
            painter.setBrush(Qt.NoBrush)

        elif device_type == "grandmaster":

            painter.setBrush(QColor("#DAA520"))
            painter.drawEllipse(QRectF(x - 14, y - 14, 28, 28))
            painter.setBrush(Qt.NoBrush)

            # Clock hands
            hand_pen = QPen(QColor("black"))
            hand_pen.setWidth(2)
            painter.setPen(hand_pen)
            painter.drawLine(QPointF(x, y), QPointF(x, y - 9))
            painter.drawLine(QPointF(x, y), QPointF(x + 6, y + 2))

            painter.setBrush(QColor("black"))
            painter.drawEllipse(QRectF(x - 1.5, y - 1.5, 3, 3))
            painter.setBrush(Qt.NoBrush)

        else:  # end_system

            painter.setBrush(QColor("lightblue"))
            painter.drawRect(QRectF(x - 13, y - 11, 26, 17))  # screen
            painter.setBrush(QColor("#666666"))
            painter.drawRect(QRectF(x - 3, y + 6, 6, 4))  # stand neck
            painter.drawRect(QRectF(x - 9, y + 10, 18, 3))  # base
            painter.setBrush(Qt.NoBrush)

    # =====================================================
    # COORDINATE CONVERSION
    # =====================================================

    def physical_to_canvas(self, x, y):
        px = self.origin_x + x * self.scale
        py = self.origin_y + y * self.scale
        return px, py

    def canvas_to_physical(self, px, py):
        x = (px - self.origin_x) / self.scale
        y = (py - self.origin_y) / self.scale
        return x, y

    # =====================================================
    # MODES
    # =====================================================

    def set_mode(self, mode):

        self.mode = mode

        names = {
            "select": "Select",
            "place_es": "Place End System",
            "place_switch": "Place Switch",
            "place_grandmaster": "Place GrandMaster",
            "link": "Create Link",
            # "delete": "Delete",
        }

        self.status.setText("Mode: " + names[mode])

        self.link_start = None

    # =====================================================
    # CANVAS CLICK
    # =====================================================

    def _default_clock_fields(self):
        """Standard clock / oscillator model parameters, shared by every
        device type. These mirror INET's real submodule parameters
        (Ieee8021qTimeAwareShaper's clock, OscillatorBasedClock,
        ConstantDriftOscillator / RandomDriftOscillator) so they can be
        emitted into omnetpp.ini as-is once NED/ini generation is added:

            *.*.clock.oscillator.typename       <- oscillator_type
            **.oscillator.nominalTickLength     <- nominal_tick_length
            *.*.clock.oscillator.driftRate      <- drift_rate  (Constant)
            **.oscillator.driftRateChangeLowerLimit  <- drift_change_lower  (Random)
            **.oscillator.driftRateChangeUpperLimit  <- drift_change_upper  (Random)
            **.oscillator.changeInterval             <- drift_change_interval (Random)
        """

        return {
            "oscillator_type": "ConstantDriftOscillator",
            "nominal_tick_length": "10ns",
            "drift_rate": "100ppm",
            "drift_change_lower": "-1ppm",
            "drift_change_upper": "1ppm",
            "drift_change_interval": "100us",
        }

    def _next_device_id(self, prefix, counter_attr):
        """Builds a fresh, currently-unused id like 'ES3'.

        The counter only needs to skip ahead when the plain next number
        is already taken -- which can happen after loading a project or
        renaming a device to look like a future auto-generated name.
        """

        count = getattr(self, counter_attr)
        while True:
            count += 1
            candidate = f"{prefix}{count}"
            if candidate not in self.devices:
                setattr(self, counter_attr, count)
                return candidate

    def canvas_click(self, event):

        device = self.find_device(event.x, event.y)

        # Place End System
        if self.mode == "place_es":

            x, y = self.canvas_to_physical(event.x, event.y)
            x = self.snap(x)
            y = self.snap(y)

            device_id = self._next_device_id("ES", "es_count")

            self.devices[device_id] = {
                "type": "end_system",
                "x": x,
                "y": y,
                "ports": 0,
                "port_speed": "1Gbps",
                "queues": 8,
                "clock_type": "802.1AS",
                **self._default_clock_fields(),
                "has_time_synchronization": False,
                "has_ingress_traffic_filtering": False,
                "has_egress_traffic_filtering": False,
                "has_egress_traffic_shaping": False,
                "has_stream_redundancy": False,
                "has_incoming_streams": False,
                "has_outgoing_streams": False,
                "has_frame_preemption": False,
                "has_cutthrough_switching": False,
                "gptp_node_type": "SLAVE_NODE",
            }

            # Select the device just placed and drop back into Select
            # mode, so it can be dragged into position right away instead
            # of the next click placing another device on top of it.
            self.selected_device = device_id
            self.redraw_network()
            self.set_mode("select")

        # Place switch
        elif self.mode == "place_switch":

            x, y = self.canvas_to_physical(event.x, event.y)
            x = self.snap(x)
            y = self.snap(y)

            device_id = self._next_device_id("SW", "sw_count")

            self.devices[device_id] = {
                "type": "switch",
                "x": x,
                "y": y,
                "ports": 0,
                "port_speed": "1Gbps",
                "queues": 8,
                "clock_type": "802.1AS",
                **self._default_clock_fields(),
                "has_time_synchronization": False,
                "has_ingress_traffic_filtering": False,
                "has_egress_traffic_shaping": False,
                "has_stream_redundancy": False,
                "has_incoming_streams": False,
                "has_outgoing_streams": False,
                "has_frame_preemption": False,
                "has_cutthrough_switching": False,
                "has_gptp": False,
                "gptp_node_type": "BRIDGE_NODE",
                "gptp_slave_port": "eth0",
            }

            self.selected_device = device_id
            self.redraw_network()
            self.set_mode("select")

        # Place GrandMaster (TsnClock)
        elif self.mode == "place_grandmaster":

            x, y = self.canvas_to_physical(event.x, event.y)
            x = self.snap(x)
            y = self.snap(y)

            device_id = self._next_device_id("GM", "gm_count")

            self.devices[device_id] = {
                "type": "grandmaster",
                "x": x,
                "y": y,
                "ports": 0,
                "port_speed": "1Gbps",
                "queues": 8,
                "clock_type": "802.1AS",
                **self._default_clock_fields(),
                # A grandmaster's whole purpose is time sync, so this
                # defaults on (every other device type defaults off).
                "has_time_synchronization": True,
                "has_ingress_traffic_filtering": False,
                "has_egress_traffic_shaping": False,
                "has_stream_redundancy": False,
                "has_incoming_streams": False,
                "has_outgoing_streams": False,
                "has_frame_preemption": False,
                "has_cutthrough_switching": False,
                "gptp_node_type": "MASTER_NODE",
            }

            self.selected_device = device_id
            self.redraw_network()
            self.set_mode("select")

        # Create link
        elif self.mode == "link":

            if device:
                if self.link_start is None:
                    self.link_start = device
                    self.status.setText(f"Link start: {device}. Select destination.")
                else:
                    if device != self.link_start:
                        self.create_link(self.link_start, device)
                    self.link_start = None
                    self.status.setText("Mode: Create Link")

        # Select
        elif self.mode == "select":

            if device:
                self.selected_device = device
                self.show_device_info(device)
            else:
                self.selected_device = None
                self.info_text.clear()

            self.redraw_network()

        # Delete
        # elif self.mode == "delete":

        #     if device:
        #         self.selected_device = device
        #         self.delete_device(device)
        #     else:
        #         link_index = self.find_link(event.x, event.y)
        #         if link_index is not None:
        #             self.delete_link(link_index)

    # =====================================================
    # SNAP
    # =====================================================

    def snap(self, value):

        if self.grid_size <= 0:
            return value

        snapped = round(value / self.grid_size) * self.grid_size
        return round(snapped, 10)

    # =====================================================
    # FIND DEVICE / FIND LINK
    # =====================================================

    def find_device(self, px, py):

        for device_id, data in self.devices.items():
            dx, dy = self.physical_to_canvas(data["x"], data["y"])
            distance = ((px - dx) ** 2 + (py - dy) ** 2) ** 0.5
            if distance <= 20:
                return device_id

        return None

    def find_link(self, px, py):
        """Return the index of the link nearest to a canvas point."""

        best_index = None
        best_distance = float("inf")

        pair_groups = {}
        for link in self.links:
            pair = frozenset([link["source"], link["destination"]])
            pair_groups.setdefault(pair, []).append(link)

        for index, link in enumerate(self.links):
            if link["source"] not in self.devices or link["destination"] not in self.devices:
                continue

            pair_devices = sorted([link["source"], link["destination"]])
            s = self.devices[pair_devices[0]]
            d = self.devices[pair_devices[1]]

            x1, y1 = self.physical_to_canvas(s["x"], s["y"])
            x2, y2 = self.physical_to_canvas(d["x"], d["y"])

            dx = x2 - x1
            dy = y2 - y1
            length = math.hypot(dx, dy) or 1
            nx = -dy / length
            ny = dx / length

            group = pair_groups[frozenset([link["source"], link["destination"]])]
            group_size = len(group)
            index_in_group = next(i for i, item in enumerate(group) if item is link)

            curve_spacing = 45
            bow = (index_in_group - (group_size - 1) / 2) * curve_spacing

            if group_size > 1:
                cx = (x1 + x2) / 2 + nx * bow
                cy = (y1 + y2) / 2 + ny * bow
                previous = (x1, y1)
                steps = 30
                for step in range(1, steps + 1):
                    t = step / steps
                    mt = 1 - t
                    current = (
                        mt * mt * x1 + 2 * mt * t * cx + t * t * x2,
                        mt * mt * y1 + 2 * mt * t * cy + t * t * y2,
                    )
                    distance = self.point_to_segment_distance(
                        px, py, previous[0], previous[1], current[0], current[1]
                    )
                    best_distance = min(best_distance, distance)
                    if distance <= 10:
                        best_index = index
                    previous = current
            else:
                distance = self.point_to_segment_distance(px, py, x1, y1, x2, y2)
                if distance < best_distance:
                    best_distance = distance
                    best_index = index if distance <= 10 else best_index

        return best_index

    def point_to_segment_distance(self, px, py, x1, y1, x2, y2):

        dx = x2 - x1
        dy = y2 - y1

        if dx == 0 and dy == 0:
            return math.hypot(px - x1, py - y1)

        t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))

        closest_x = x1 + t * dx
        closest_y = y1 + t * dy
        return math.hypot(px - closest_x, py - closest_y)

    # =====================================================
    # PORT MANAGEMENT
    # =====================================================

    def get_connected_link_count(self, device_id):
        """Return the number of physical links connected to a device."""

        return sum(
            1
            for link in self.links
            if link["source"] == device_id or link["destination"] == device_id
        )

    def ensure_minimum_ports(self, device_id):
        """Increase ports automatically when more links are connected.

        This method NEVER decreases ports. Decreases are handled only by
        an explicit link deletion, as requested.
        """

        if device_id not in self.devices:
            return

        required = self.get_connected_link_count(device_id)

        try:
            current = int(float(self.devices[device_id].get("ports", 0)))
        except (TypeError, ValueError):
            current = 0

        self.devices[device_id]["ports"] = max(current, required, 0)

    def validate_port_count(self, device_id, value):
        """Allow manual increases only; manual decreases are forbidden."""

        try:
            ports = int(value)
        except (TypeError, ValueError):
            QMessageBox.critical(
                self, "Invalid Port Count", "Number of ports must be a non-negative integer."
            )
            return None

        if device_id not in self.devices:
            return None

        try:
            current = int(float(self.devices[device_id].get("ports", 0)))
        except (TypeError, ValueError):
            current = 0

        required = self.get_connected_link_count(device_id)

        if ports < current:
            QMessageBox.warning(
                self,
                "Port Count Cannot Be Decreased",
                f"{device_id} currently has {current} port(s).\n\n"
                "You can increase the number of ports manually, but you "
                "cannot decrease it manually.\n\n"
                "Ports are decreased automatically only when a connected "
                "link is deleted.",
            )
            return None

        if ports < required:
            QMessageBox.warning(
                self,
                "Too Few Ports",
                f"{device_id} has {required} connected link(s).\n\n"
                f"The number of ports cannot be less than {required}.",
            )
            return None

        if ports < 0:
            QMessageBox.warning(
                self, "Invalid Port Count", "The number of ports cannot be negative."
            )
            return None

        return ports

    def recalculate_ports_after_link_deletion(self, affected_devices):
        """After a link is deleted, set affected devices to their new
        connected-link count. This is the ONLY automatic decrease path."""

        for device_id in affected_devices:
            if device_id in self.devices:
                self.devices[device_id]["ports"] = self.get_connected_link_count(device_id)

    def rename_device(self, old_id, new_id):
        """Renames a device and updates every place that refers to it by
        id: its own dict key, every link's source/destination, every
        flow's source/destination, the current selection, and an
        in-progress link start point. Does nothing if the id is unchanged."""

        if old_id == new_id:
            return

        data = self.devices.pop(old_id)
        self.devices[new_id] = data

        for link in self.links:
            if link["source"] == old_id:
                link["source"] = new_id
            if link["destination"] == old_id:
                link["destination"] = new_id

        for flow in self.flows:
            if flow["source"] == old_id:
                flow["source"] = new_id
            if flow["destination"] == old_id:
                flow["destination"] = new_id

        if self.selected_device == old_id:
            self.selected_device = new_id

        if self.link_start == old_id:
            self.link_start = new_id

    # =====================================================
    # CREATE LINK
    # =====================================================

    def create_link(self, source, destination):

        existing = [
            link
            for link in self.links
            if (link["source"] == source and link["destination"] == destination)
            or (link["source"] == destination and link["destination"] == source)
        ]

        if existing:
            proceed = QMessageBox.question(
                self,
                "Redundant Link",
                f"{len(existing)} link(s) already exist between "
                f"{source} and {destination}.\n\n"
                "Add a redundant parallel link anyway?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if proceed != QMessageBox.Yes:
                return

        self.link_count += 1

        self.links.append(
            {
                "link_id": f"L{self.link_count}",
                "source": source,
                "destination": destination,
                "bitrate": "1Gbps",
                "delay": "0.001",
                "duplex": "full",
                "length": "auto",
                "redundant": bool(existing),
            }
        )

        self.ensure_minimum_ports(source)
        self.ensure_minimum_ports(destination)

        self.redraw_network()

    # =====================================================
    # REDRAW  (schedules a repaint; see paint_network below)
    # =====================================================

    def redraw_network(self):

        for device_id in self.devices:
            self.ensure_minimum_ports(device_id)

        self.update_flow_list()
        self.canvas.update()

    # =====================================================
    # PAINT NETWORK -- everything that used to be canvas.create_*()
    # calls inside redraw_network() now lives here, run from
    # NetworkCanvas.paintEvent().
    # =====================================================

    def paint_network(self, painter: QPainter, canvas_width, canvas_height):

        self._compute_grid_geometry(canvas_width, canvas_height)

        self.draw_grid(painter)

        # ---- links ----

        pair_groups = {}
        for link in self.links:
            pair = frozenset([link["source"], link["destination"]])
            pair_groups.setdefault(pair, []).append(link)

        for link in self.links:

            if link["source"] not in self.devices or link["destination"] not in self.devices:
                continue

            pair_devices = sorted([link["source"], link["destination"]])
            canonical_source = self.devices[pair_devices[0]]
            canonical_destination = self.devices[pair_devices[1]]

            x1, y1 = self.physical_to_canvas(canonical_source["x"], canonical_source["y"])
            x2, y2 = self.physical_to_canvas(canonical_destination["x"], canonical_destination["y"])

            pair = frozenset([link["source"], link["destination"]])
            group = pair_groups[pair]
            group_size = len(group)
            index_in_group = next(i for i, l in enumerate(group) if l is link)

            dx = x2 - x1
            dy = y2 - y1
            length = math.hypot(dx, dy) or 1
            nx = -dy / length
            ny = dx / length

            if group_size > 1:
                curve_spacing = 45
                bow = (index_in_group - (group_size - 1) / 2) * curve_spacing
            else:
                bow = 0

            cx = (x1 + x2) / 2 + nx * bow
            cy = (y1 + y2) / 2 + ny * bow

            line_color = "darkgreen" if link.get("redundant") else "black"
            line_dash = (6, 3) if link.get("redundant") else None

            if group_size > 1:
                points = []
                steps = 30
                for step in range(steps + 1):
                    t = step / steps
                    mt = 1 - t
                    px = mt * mt * x1 + 2 * mt * t * cx + t * t * x2
                    py = mt * mt * y1 + 2 * mt * t * cy + t * t * y2
                    points.extend([px, py])
                self._draw_polyline(painter, points, line_color, 3, line_dash)
            else:
                self._draw_polyline(painter, [x1, y1, x2, y2], line_color, 3, line_dash)

            label_push = bow + (18 if bow >= 0 else -18)
            lx = (x1 + x2) / 2 + nx * label_push
            ly = (y1 + y2) / 2 + ny * label_push

            self._draw_centered_text(
                painter, lx, ly, link["bitrate"], "black", QFont("Arial", 9)
            )

            if link.get("redundant"):
                self._draw_centered_text(
                    painter,
                    lx,
                    ly + (14 if bow >= 0 else -14),
                    f"redundant [{link.get('link_id', '')}]",
                    "darkgreen",
                    QFont("Arial", 8, italic=True),
                )

        self.draw_link_legend(painter)

        self.draw_flows(painter)
        self.draw_flow_legend(painter, canvas_width)

        # ---- devices ----

        for device_id, data in self.devices.items():

            x, y = self.physical_to_canvas(data["x"], data["y"])

            self._draw_device_icon(painter, data["type"], x, y)

            self._draw_centered_text(
                painter, x, y + 25, device_id, "black", QFont("Arial", 9, QFont.Bold)
            )

        # ---- selection highlight ----

        if self.selected_device in self.devices:
            data = self.devices[self.selected_device]
            x, y = self.physical_to_canvas(data["x"], data["y"])
            pen = QPen(QColor("red"))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QRectF(x - 18, y - 18, 36, 36))

    # =====================================================
    # LINK LEGEND
    # =====================================================

    def draw_link_legend(self, painter: QPainter):

        lx = 15
        ly = 15

        pen = QPen(QColor("#999999"))
        painter.setPen(pen)
        painter.setBrush(QColor("white"))
        painter.drawRect(QRectF(lx - 8, ly - 8, 178, 54))
        painter.setBrush(Qt.NoBrush)

        self._draw_polyline(painter, [lx, ly, lx + 30, ly], "black", 3)
        self._draw_centered_text(painter, lx + 95, ly, "Primary link", "black", QFont("Arial", 8))

        self._draw_polyline(painter, [lx, ly + 20, lx + 30, ly + 20], "darkgreen", 3, (6, 3))
        self._draw_centered_text(
            painter, lx + 100, ly + 20, "Redundant link", "black", QFont("Arial", 8)
        )

    # =====================================================
    # DRAW FLOWS
    # =====================================================

    def draw_flows(self, painter: QPainter):

        if not self.flows:
            return

        if not self.show_flows:
            return

        pair_groups = {}
        for flow in self.flows:
            if flow["source"] not in self.devices or flow["destination"] not in self.devices:
                continue
            pair = frozenset([flow["source"], flow["destination"]])
            pair_groups.setdefault(pair, []).append(flow)

        for flow in self.flows:

            if flow["source"] not in self.devices or flow["destination"] not in self.devices:
                continue

            pair_devices = sorted([flow["source"], flow["destination"]])
            canonical_source = self.devices[pair_devices[0]]
            canonical_destination = self.devices[pair_devices[1]]

            x1, y1 = self.physical_to_canvas(canonical_source["x"], canonical_source["y"])
            x2, y2 = self.physical_to_canvas(canonical_destination["x"], canonical_destination["y"])

            pair = frozenset([flow["source"], flow["destination"]])
            group = pair_groups[pair]
            group_size = len(group)
            index_in_group = next(i for i, f in enumerate(group) if f is flow)

            dx = x2 - x1
            dy = y2 - y1
            length = math.hypot(dx, dy) or 1
            nx = -dy / length
            ny = dx / length

            base_offset = 34
            spacing = 22
            bow = base_offset + (index_in_group - (group_size - 1) / 2) * spacing

            cx = (x1 + x2) / 2 + nx * bow
            cy = (y1 + y2) / 2 + ny * bow

            points = []
            steps = 30
            for step in range(steps + 1):
                t = step / steps
                mt = 1 - t
                px = mt * mt * x1 + 2 * mt * t * cx + t * t * x2
                py = mt * mt * y1 + 2 * mt * t * cy + t * t * y2
                points.extend([px, py])

            if flow["source"] != pair_devices[0]:
                reversed_points = []
                for i in range(len(points) - 2, -2, -2):
                    reversed_points.extend([points[i], points[i + 1]])
                points = reversed_points

            color = flow.get("color", "#4363d8")
            dash = self.FLOW_DASH_BY_TRAFFIC.get(flow.get("traffic_type"))
            width = self.FLOW_WIDTH_BY_TRAFFIC.get(flow.get("traffic_type"), 2)

            self._draw_polyline(painter, points, color, width, dash)

            # arrowhead at the final segment, pointing at the flow's destination
            self._draw_arrowhead(
                painter, points[-4], points[-3], points[-2], points[-1], color
            )

            mid_x = 0.25 * x1 + 0.5 * cx + 0.25 * x2
            mid_y = 0.25 * y1 + 0.5 * cy + 0.25 * y2

            self._draw_centered_text(
                painter, mid_x, mid_y, f"F{flow['flow_id']}", color, QFont("Arial", 8, QFont.Bold)
            )

    # =====================================================
    # FLOW LEGEND
    # =====================================================

    def draw_flow_legend(self, painter: QPainter, canvas_width):

        if not self.flows:
            return

        if not self.show_flows:
            return

        if canvas_width < 100:
            canvas_width = 850

        entries = [
            (key, self.FLOW_DASH_BY_TRAFFIC[key], self.FLOW_WIDTH_BY_TRAFFIC[key])
            for key in self.FLOW_TRAFFIC_TYPES
        ]

        lx = canvas_width - 195
        ly = 15

        pen = QPen(QColor("#999999"))
        painter.setPen(pen)
        painter.setBrush(QColor("white"))
        painter.drawRect(QRectF(lx - 8, ly - 8, 193, 20 * len(entries) + 12))
        painter.setBrush(Qt.NoBrush)

        for i, (label, dash, width) in enumerate(entries):
            yy = ly + i * 20
            self._draw_polyline(painter, [lx, yy, lx + 30, yy], "#555555", width, dash)
            self._draw_centered_text(
                painter, lx + 115, yy, f"{label} flow", "black", QFont("Arial", 8)
            )

    # =====================================================
    # FLOW LIST (side panel)
    # =====================================================

    def update_flow_list(self):

        if not hasattr(self, "flow_listbox"):
            return

        self.flow_listbox.clear()

        for flow in self.flows:
            label = (
                f"F{flow['flow_id']}: {flow['source']} -> "
                f"{flow['destination']}  [{flow['traffic_type']}]"
            )
            item_text = label
            self.flow_listbox.addItem(item_text)
            item = self.flow_listbox.item(self.flow_listbox.count() - 1)
            item.setForeground(QColor(flow.get("color", "black")))

    def on_flow_listbox_double_click(self, item):

        index = self.flow_listbox.row(item)

        if index < 0:
            return

        self.edit_flow(preselect_index=index)

    # =====================================================
    # SHOW DEVICE INFO
    # =====================================================

    def show_device_info(self, device_id):

        data = self.devices[device_id]

        self.info_text.clear()

        lines = [f"{key}: {value}" for key, value in data.items()]
        self.info_text.setPlainText("\n".join(lines) + ("\n" if lines else ""))

    # =====================================================
    # EDIT DEVICE
    # =====================================================

    def edit_device(self):

        if not self.selected_device:
            QMessageBox.information(self, "Device", "Select a device first.")
            return

        data = self.devices[self.selected_device]

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Edit {self.selected_device}")
        layout = QGridLayout(dialog)

        fields = {}

        device_type = data.get("type", "end_system")

        common_fields = [
            "ports",
            "port_speed",
            "queues",
            "clock_type",
            # Standard clock / oscillator model parameters -- shared by
            # every device type, since every node with time sync enabled
            # gets its own clock submodule in INET.
            "oscillator_type",
            "nominal_tick_length",
            "drift_rate",
            "drift_change_lower",
            "drift_change_upper",
            "drift_change_interval",
            "has_time_synchronization",
            "has_ingress_traffic_filtering",
            "has_egress_traffic_shaping",
            "has_stream_redundancy",
            "has_incoming_streams",
            "has_outgoing_streams",
            "has_frame_preemption",
            "has_cutthrough_switching",
            "gptp_node_type",
        ]

        if device_type == "switch":
            editable = common_fields + ["has_gptp", "gptp_slave_port"]
        elif device_type == "grandmaster":
            # Simplest of the three: no bridge-only gPTP fields (a
            # grandmaster is the master, it has no "slave port"), and no
            # extra egress-filtering leg (that's an end-system-only field).
            editable = common_fields
        else:
            # end_system: gets BOTH ingress and egress traffic filtering
            # (802.1Qci both directions), unlike a switch which only
            # filters ingress. Insert right after has_ingress_traffic_filtering,
            # found by name rather than a hardcoded index so this keeps
            # working if common_fields above ever grows or reorders.
            insert_at = common_fields.index("has_ingress_traffic_filtering") + 1
            editable = (
                common_fields[:insert_at]
                + ["has_egress_traffic_filtering"]
                + common_fields[insert_at:]
            )

        try:
            port_count = int(float(data.get("ports", 0)))
        except (TypeError, ValueError):
            port_count = 0

        port_count = max(port_count, 1)
        eth_port_options = [f"eth{i}" for i in range(port_count)]

        dropdown_options = {
            "port_speed": ["10Mbps", "100Mbps", "1Gbps", "2.5Gbps", "5Gbps", "10Gbps"],
            "clock_type": ["802.1AS", "PTPv2", "None"],
            "oscillator_type": [
                "ConstantDriftOscillator",
                "RandomDriftOscillator",
                "IdealOscillator",
            ],
            "has_time_synchronization": ["True", "False"],
            "has_ingress_traffic_filtering": ["True", "False"],
            "has_egress_traffic_filtering": ["True", "False"],
            "has_egress_traffic_shaping": ["True", "False"],
            "has_stream_redundancy": ["True", "False"],
            "has_incoming_streams": ["True", "False"],
            "has_outgoing_streams": ["True", "False"],
            "has_frame_preemption": ["True", "False"],
            "has_cutthrough_switching": ["True", "False"],
            "has_gptp": ["True", "False"],
            "gptp_node_type": ["SLAVE_NODE", "MASTER_NODE", "BRIDGE_NODE"],
            "gptp_slave_port": eth_port_options,
        }

        layout.addWidget(QLabel("name"), 0, 0)
        name_field = QLineEdit(self.selected_device)
        layout.addWidget(name_field, 0, 1)

        for offset, key in enumerate(editable):

            row = offset + 1

            label_text = (
                "ports (increase only; link deletion can decrease)"
                if key == "ports"
                else key
            )
            layout.addWidget(QLabel(label_text), row, 0)

            current_value = str(data.get(key, ""))

            if key in dropdown_options:
                options = dropdown_options[key]
                widget = QComboBox()
                widget.addItems(options)
                if current_value in options:
                    widget.setCurrentText(current_value)
                elif options:
                    widget.setCurrentIndex(0)
            else:
                widget = QLineEdit(current_value)

            layout.addWidget(widget, row, 1)
            fields[key] = widget

        def save():

            new_name = name_field.text().strip()

            if not new_name:
                QMessageBox.critical(self, "Invalid Name", "Device name cannot be empty.")
                return

            if new_name != self.selected_device and new_name in self.devices:
                QMessageBox.critical(
                    self,
                    "Duplicate Name",
                    f"A device named '{new_name}' already exists. "
                    "Device names must be unique.",
                )
                return

            self.rename_device(self.selected_device, new_name)
            current_id = self.selected_device

            if "ports" in fields:
                raw = fields["ports"].text()
                validated_ports = self.validate_port_count(current_id, raw)
                if validated_ports is None:
                    return
                data["ports"] = validated_ports

            for key, widget in fields.items():
                if key == "ports":
                    continue
                if isinstance(widget, QComboBox):
                    data[key] = widget.currentText()
                else:
                    data[key] = widget.text()

            self.show_device_info(current_id)
            dialog.close()
            self.redraw_network()

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(save)
        layout.addWidget(save_btn, len(editable) + 1, 0, 1, 2)

        self._open_dialogs.append(dialog)
        dialog.show()

    # =====================================================
    # EDIT LINK
    # =====================================================

    def edit_link(self, preselect_index=0):

        if len(self.links) == 0:
            QMessageBox.information(self, "Link", "No links exist.")
            return

        if not (0 <= preselect_index < len(self.links)):
            preselect_index = 0

        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Link")
        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel("Select Link"))

        link_names = []
        for link in self.links:
            label = f"{link['source']} -> {link['destination']}"
            if link.get("link_id"):
                label += f"  [{link['link_id']}]"
            if link.get("redundant"):
                label += "  (redundant)"
            link_names.append(label)

        combo = QComboBox()
        combo.addItems(link_names)
        combo.setCurrentIndex(preselect_index)
        layout.addWidget(combo)

        layout.addWidget(QLabel("Bitrate:"))
        bitrate = QLineEdit(self.links[preselect_index]["bitrate"])
        layout.addWidget(bitrate)

        layout.addWidget(QLabel("Delay:"))
        delay = QLineEdit(self.links[preselect_index]["delay"])
        layout.addWidget(delay)

        def on_select(index):
            if 0 <= index < len(self.links):
                bitrate.setText(self.links[index]["bitrate"])
                delay.setText(self.links[index]["delay"])

        combo.currentIndexChanged.connect(on_select)

        def save():
            index = combo.currentIndex()
            self.links[index]["bitrate"] = bitrate.text()
            self.links[index]["delay"] = delay.text()
            dialog.close()
            self.redraw_network()

        def delete():
            idx = combo.currentIndex()
            self.delete_link(idx)
            dialog.close()

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(save)
        layout.addWidget(save_btn)

        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(delete)
        layout.addWidget(delete_btn)

        self._open_dialogs.append(dialog)
        dialog.show()

    # =====================================================
    # SHOW REDUNDANT LINKS
    # =====================================================

    def show_redundant_links(self):

        redundant = [link for link in self.links if link.get("redundant")]

        dialog = QDialog(self)
        dialog.setWindowTitle("Redundant Links")
        dialog.resize(400, 300)
        layout = QVBoxLayout(dialog)

        title = QLabel("Redundant / Parallel Links")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        if not redundant:
            layout.addWidget(QLabel("No redundant links in the current topology."))
            self._open_dialogs.append(dialog)
            dialog.show()
            return

        listbox = QListWidget()
        layout.addWidget(listbox, 1)

        pair_groups = {}
        for link in self.links:
            pair = frozenset([link["source"], link["destination"]])
            pair_groups.setdefault(pair, []).append(link)

        for pair, group in pair_groups.items():

            if len(group) < 2:
                continue

            a, b = tuple(pair) if len(pair) == 2 else (list(pair)[0], list(pair)[0])

            listbox.addItem(f"--- {a} <-> {b} ({len(group)} links) ---")

            for link in group:
                tag = "redundant" if link.get("redundant") else "primary"
                listbox.addItem(
                    f"   [{link.get('link_id', '?')}] "
                    f"{link['source']} -> {link['destination']} "
                    f"  ({tag}, {link['bitrate']})"
                )

        def edit_selected(item):
            text = item.text()
            if "[" not in text or "]" not in text:
                return
            link_id = text.split("[")[1].split("]")[0]
            for i, link in enumerate(self.links):
                if link.get("link_id") == link_id:
                    dialog.close()
                    self.edit_link(preselect_index=i)
                    return

        listbox.itemDoubleClicked.connect(edit_selected)

        hint = QLabel("Double-click a link to edit it.")
        hint.setStyleSheet("font-style: italic; font-size: 8pt;")
        layout.addWidget(hint)

        self._open_dialogs.append(dialog)
        dialog.show()

    def delete_link(self, link_index):
        """Delete one link and automatically reduce both endpoint port
        counts to their new connected-link counts."""

        if not (0 <= link_index < len(self.links)):
            return

        link = self.links[link_index]
        affected_devices = {link["source"], link["destination"]}

        proceed = QMessageBox.question(
            self,
            "Delete Link",
            f"Delete {link.get('link_id', 'this link')} "
            f"({link['source']} -> {link['destination']})?",
            QMessageBox.Yes | QMessageBox.No,
        )

        if proceed != QMessageBox.Yes:
            return

        del self.links[link_index]

        self.recalculate_ports_after_link_deletion(affected_devices)

        self.redraw_network()

    # =====================================================
    # DELETE DEVICE
    # =====================================================

    def delete_device(self, device_id):

        if device_id not in self.devices:
            return

        affected_devices = set()
        for link in self.links:
            if link["source"] == device_id and link["destination"] != device_id:
                affected_devices.add(link["destination"])
            elif link["destination"] == device_id and link["source"] != device_id:
                affected_devices.add(link["source"])

        del self.devices[device_id]

        self.links = [
            link
            for link in self.links
            if link["source"] != device_id and link["destination"] != device_id
        ]

        self.recalculate_ports_after_link_deletion(affected_devices)

        self.selected_device = None
        self.info_text.clear()

        self.redraw_network()

    # =====================================================
    # DELETE SELECTED
    # =====================================================

    def delete_selected(self):

        if self.selected_device:
            self.delete_device(self.selected_device)

    # =====================================================
    # DRAG DEVICE
    # =====================================================

    def drag_device(self, event):

        if self.mode != "select":
            return

        # device = self.find_device(event.x, event.y)
        device = self.dragging_device

        if device not in self.devices:
            return

        # if device:
        # self.selected_device = device

        x, y = self.canvas_to_physical(event.x, event.y)

        x = max(0, min(self.area_width, x))
        y = max(0, min(self.area_height, y))

        self.devices[device]["x"] = self.snap(x)
        self.devices[device]["y"] = self.snap(y)

        self.redraw_network()

    def release_device(self, event):
        if self.dragging_device in self.devices:
            self.selected_device = self.dragging_device
            self.show_device_info(self.selected_device)

        self.dragging_device = None

    # Added for dragging device funtionality
    def begin_mouse_press(self, event):
        if self.mode == 'select':
            self.dragging_device = self.find_device(event.x, event.y)

            if self.dragging_device:
                self.selected_device = self.dragging_device
                self.show_device_info(self.dragging_device)
            else:
                self.selected_device = None
                self.info_text.clear()

            self.redraw_network()

        else:
            self.canvas_click(event)

    # =====================================================
    # ADD FLOW
    # =====================================================

    def add_flow(self):

        if len(self.devices) < 2:
            QMessageBox.information(
                self,
                "Add Flow",
                "Add at least two devices (a source and a "
                "destination) before creating a flow.",
            )
            return

        device_ids = sorted(self.devices.keys())

        dialog = QDialog(self)
        dialog.setWindowTitle("Add Flow")
        layout = QGridLayout(dialog)

        title = QLabel("New Flow")
        title.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(title, 0, 0, 1, 2)

        fields = {}
        row = 1

        layout.addWidget(QLabel("Source:"), row, 0)
        source_combo = QComboBox()
        source_combo.addItems(device_ids)
        source_combo.setCurrentIndex(0)
        layout.addWidget(source_combo, row, 1)
        fields["source"] = source_combo
        row += 1

        layout.addWidget(QLabel("Destination:"), row, 0)
        destination_combo = QComboBox()
        destination_combo.addItems(device_ids)
        destination_combo.setCurrentIndex(1 if len(device_ids) > 1 else 0)
        layout.addWidget(destination_combo, row, 1)
        fields["destination"] = destination_combo
        row += 1

        layout.addWidget(QLabel("Traffic Type:"), row, 0)
        traffic_combo = QComboBox()
        traffic_combo.addItems(self.FLOW_TRAFFIC_TYPES)
        traffic_combo.setCurrentText("BE")
        layout.addWidget(traffic_combo, row, 1)
        fields["traffic_type"] = traffic_combo
        row += 1

        layout.addWidget(QLabel("PCP / Queue No:"), row, 0)
        pcp_combo = QComboBox()
        pcp_combo.addItems(self.FLOW_PCP_QUEUES)
        pcp_combo.setCurrentText("0")
        layout.addWidget(pcp_combo, row, 1)
        fields["pcp_queue_no"] = pcp_combo
        row += 1

        for key, label, default in self.FLOW_ENTRY_FIELDS:
            layout.addWidget(QLabel(label), row, 0)
            entry = QLineEdit(default)
            layout.addWidget(entry, row, 1)
            fields[key] = entry
            row += 1

        layout.addWidget(QLabel("Reliability (0-1]:"), row, 0)
        reliability_combo = QComboBox()
        reliability_combo.setEditable(True)
        reliability_combo.addItems(self.FLOW_RELIABILITY_OPTIONS)
        reliability_combo.setCurrentText("0.95")
        layout.addWidget(reliability_combo, row, 1)
        fields["reliability"] = reliability_combo
        row += 1

        label_lookup = {key: label for key, label, _ in self.FLOW_ENTRY_FIELDS}
        label_lookup["reliability"] = "Reliability"

        def get_text(widget):
            return widget.currentText() if isinstance(widget, QComboBox) else widget.text()

        def save():

            source = get_text(fields["source"])
            destination = get_text(fields["destination"])

            if not source or not destination:
                QMessageBox.critical(
                    self, "Invalid Flow", "Select both a source and a destination."
                )
                return

            if source == destination:
                QMessageBox.critical(
                    self,
                    "Invalid Flow",
                    "Source and destination must be different devices.",
                )
                return

            numeric_keys = [key for key, _, _ in self.FLOW_ENTRY_FIELDS] + ["reliability"]

            values = {}
            for key in numeric_keys:
                raw = get_text(fields[key]).strip()
                try:
                    values[key] = float(raw)
                except ValueError:
                    QMessageBox.critical(
                        self, "Invalid Value", f"'{label_lookup[key]}' must be numeric."
                    )
                    return

            if not (0 < values["reliability"] <= 1):
                QMessageBox.critical(
                    self,
                    "Invalid Value",
                    "Reliability must be a value greater than 0 "
                    "and less than or equal to 1 (e.g. 0.95).",
                )
                return

            self.flow_count += 1

            self.flows.append(
                {
                    "flow_id": self.flow_count,
                    "source": source,
                    "destination": destination,
                    "traffic_type": get_text(fields["traffic_type"]),
                    "pcp_queue_no": int(get_text(fields["pcp_queue_no"])),
                    "payload_size_kb": values["payload_size_kb"],
                    "deadline_microsec": values["deadline_microsec"],
                    "stream_periodicity_microsec": values["stream_periodicity_microsec"],
                    "burst_size_mb": values["burst_size_mb"],
                    "max_jitter_microsec": values["max_jitter_microsec"],
                    "reliability": values["reliability"],
                    "bandwidth_mbps": values["bandwidth_mbps"],
                    "color": self.FLOW_COLOR_PALETTE[
                        (self.flow_count - 1) % len(self.FLOW_COLOR_PALETTE)
                    ],
                }
            )

            dialog.close()
            self.redraw_network()

            QMessageBox.information(
                self,
                "Flow Added",
                f"Flow F{self.flow_count} created: {source} -> {destination}",
            )

        save_btn = QPushButton("Add Flow")
        save_btn.clicked.connect(save)
        layout.addWidget(save_btn, row, 0, 1, 2)

        self._open_dialogs.append(dialog)
        dialog.show()

    # =====================================================
    # EDIT / DELETE FLOW
    # =====================================================

    def edit_flow(self, preselect_index=0):

        if not self.flows:
            QMessageBox.information(
                self, "Flows", "No flows exist yet. Use 'Add Flow' to create one."
            )
            return

        if not (0 <= preselect_index < len(self.flows)):
            preselect_index = 0

        device_ids = sorted(self.devices.keys())

        dialog = QDialog(self)
        dialog.setWindowTitle("Manage Flows")
        layout = QGridLayout(dialog)

        layout.addWidget(QLabel("Select Flow:"), 0, 0, 1, 2)

        flow_names = [
            f"F{flow['flow_id']}: {flow['source']} -> "
            f"{flow['destination']}  ({flow['traffic_type']})"
            for flow in self.flows
        ]

        combo = QComboBox()
        combo.addItems(flow_names)
        combo.setCurrentIndex(preselect_index)
        layout.addWidget(combo, 1, 0, 1, 2)

        fields = {}
        row = 2

        layout.addWidget(QLabel("Source:"), row, 0)
        source_combo = QComboBox()
        source_combo.addItems(device_ids)
        layout.addWidget(source_combo, row, 1)
        fields["source"] = source_combo
        row += 1

        layout.addWidget(QLabel("Destination:"), row, 0)
        destination_combo = QComboBox()
        destination_combo.addItems(device_ids)
        layout.addWidget(destination_combo, row, 1)
        fields["destination"] = destination_combo
        row += 1

        layout.addWidget(QLabel("Traffic Type:"), row, 0)
        traffic_combo = QComboBox()
        traffic_combo.addItems(self.FLOW_TRAFFIC_TYPES)
        layout.addWidget(traffic_combo, row, 1)
        fields["traffic_type"] = traffic_combo
        row += 1

        layout.addWidget(QLabel("PCP / Queue No:"), row, 0)
        pcp_combo = QComboBox()
        pcp_combo.addItems(self.FLOW_PCP_QUEUES)
        layout.addWidget(pcp_combo, row, 1)
        fields["pcp_queue_no"] = pcp_combo
        row += 1

        for key, label, _ in self.FLOW_ENTRY_FIELDS:
            layout.addWidget(QLabel(label), row, 0)
            entry = QLineEdit()
            layout.addWidget(entry, row, 1)
            fields[key] = entry
            row += 1

        layout.addWidget(QLabel("Reliability (0-1]:"), row, 0)
        reliability_combo = QComboBox()
        reliability_combo.setEditable(True)
        reliability_combo.addItems(self.FLOW_RELIABILITY_OPTIONS)
        layout.addWidget(reliability_combo, row, 1)
        fields["reliability"] = reliability_combo
        row += 1

        label_lookup = {key: label for key, label, _ in self.FLOW_ENTRY_FIELDS}
        label_lookup["reliability"] = "Reliability"

        def get_text(widget):
            return widget.currentText() if isinstance(widget, QComboBox) else widget.text()

        def set_text(widget, value):
            if isinstance(widget, QComboBox):
                widget.setCurrentText(value)
            else:
                widget.setText(value)

        def load_flow(index):

            flow = self.flows[index]

            set_text(fields["source"], flow["source"])
            set_text(fields["destination"], flow["destination"])
            set_text(fields["traffic_type"], flow["traffic_type"])
            set_text(fields["pcp_queue_no"], str(flow["pcp_queue_no"]))

            for key, _, _ in self.FLOW_ENTRY_FIELDS:
                set_text(fields[key], str(flow[key]))

            set_text(fields["reliability"], str(flow["reliability"]))

        load_flow(preselect_index)

        def on_select(index):
            if 0 <= index < len(self.flows):
                load_flow(index)

        combo.currentIndexChanged.connect(on_select)

        def save():

            index = combo.currentIndex()

            source = get_text(fields["source"])
            destination = get_text(fields["destination"])

            if not source or not destination:
                QMessageBox.critical(
                    self, "Invalid Flow", "Select both a source and a destination."
                )
                return

            if source == destination:
                QMessageBox.critical(
                    self,
                    "Invalid Flow",
                    "Source and destination must be different devices.",
                )
                return

            numeric_keys = [key for key, _, _ in self.FLOW_ENTRY_FIELDS] + ["reliability"]

            values = {}
            for key in numeric_keys:
                raw = get_text(fields[key]).strip()
                try:
                    values[key] = float(raw)
                except ValueError:
                    QMessageBox.critical(
                        self, "Invalid Value", f"'{label_lookup[key]}' must be numeric."
                    )
                    return

            if not (0 < values["reliability"] <= 1):
                QMessageBox.critical(
                    self,
                    "Invalid Value",
                    "Reliability must be a value greater than 0 "
                    "and less than or equal to 1 (e.g. 0.95).",
                )
                return

            flow = self.flows[index]

            flow["source"] = source
            flow["destination"] = destination
            flow["traffic_type"] = get_text(fields["traffic_type"])
            flow["pcp_queue_no"] = int(get_text(fields["pcp_queue_no"]))

            for key in numeric_keys:
                flow[key] = values[key]

            dialog.close()
            self.redraw_network()

            QMessageBox.information(self, "Flow Updated", f"Flow F{flow['flow_id']} updated.")

        def delete():

            index = combo.currentIndex()
            flow = self.flows[index]

            proceed = QMessageBox.question(
                self,
                "Delete Flow",
                f"Delete flow F{flow['flow_id']} "
                f"({flow['source']} -> {flow['destination']})?",
                QMessageBox.Yes | QMessageBox.No,
            )

            if proceed != QMessageBox.Yes:
                return

            del self.flows[index]

            dialog.close()
            self.redraw_network()

            QMessageBox.information(self, "Flow Deleted", "Flow removed.")

        save_btn = QPushButton("Save")
        save_btn.clicked.connect(save)
        layout.addWidget(save_btn, row, 0)

        delete_btn = QPushButton("Delete")
        delete_btn.clicked.connect(delete)
        layout.addWidget(delete_btn, row, 1)

        self._open_dialogs.append(dialog)
        dialog.show()

    # =====================================================
    # SAVE
    # =====================================================

    def _topology_dict(self):

        return {
            "area": {
                "width": self.area_width,
                "height": self.area_height,
                "grid": self.grid_size,
            },
            "devices": self.devices,
            "links": self.links,
            "flows": self.flows,
        }

    def save_topology(self):

        with open("tsn_topology.json", "w") as f:
            json.dump(self._topology_dict(), f, indent=4)

        QMessageBox.information(self, "Saved", "Topology saved as tsn_topology.json")

    # =====================================================
    # GENERATOR
    # =====================================================

    def generate_ned_ini(self):
        """Open generation settings and generate the INET NED/INI files."""

        # Make sure generation always uses the current canvas state.
        try:
            with open("tsn_topology.json", "w", encoding="utf-8") as f:
                json.dump(self._topology_dict(), f, indent=4)
        except OSError as exc:
            QMessageBox.critical(self, "Generate NED and INI", f"Could not save topology:\n{exc}")
            return

        grandmasters = [
            name for name, data in self.devices.items()
            if data.get("type") == "grandmaster"
        ]

        dialog = GenerationConfigDialog(grandmasters, self)
        if dialog.exec() != QDialog.Accepted:
            return

        config = dialog.config()
        try:
            ned, ini = generator.generate_files(
                "tsn_topology.json",
                "simulations/generated",
                config["network_name"],
                config=config,
            )
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.critical(self, "Generate NED and INI", f"Generation failed:\n{exc}")
            return

        QMessageBox.information(
            self,
            "Generation Complete",
            f"Generated successfully:\n\nNED: {ned}\nomnetpp.ini: {ini}",
        )
    

    # =====================================================
    # FILE MENU ACTIONS
    # =====================================================

    def _reset_topology_state(self):

        self.devices = {}
        self.links = []
        self.flows = []

        self.es_count = 0
        self.sw_count = 0
        self.gm_count = 0
        self.link_count = 0
        self.flow_count = 0

        self.selected_device = None
        self.link_start = None
        self.info_text.clear()

        self.set_mode("select")

    def _recompute_counters(self):
        """After loading a topology from a file, point each id counter at
        the highest number already in use for that prefix, so newly
        placed devices/links/flows don't collide with loaded ones."""

        def highest(ids, prefix):
            best = 0
            for item_id in ids:
                if item_id.startswith(prefix) and item_id[len(prefix):].isdigit():
                    best = max(best, int(item_id[len(prefix):]))
            return best

        self.es_count = highest(self.devices.keys(), "ES")
        self.sw_count = highest(self.devices.keys(), "SW")
        self.gm_count = highest(self.devices.keys(), "GM")

        self.link_count = highest(
            [link.get("link_id", "") for link in self.links], "L"
        )
        self.flow_count = max(
            [flow.get("flow_id", 0) for flow in self.flows], default=0
        )

    def _apply_area_to_ui(self):

        self.width_entry.setText(self.format_grid_value(self.area_width))
        self.height_entry.setText(self.format_grid_value(self.area_height))
        self.grid_entry.setText(self.format_grid_value(self.grid_size))

    def _load_topology_dict(self, data):

        area = data.get("area", {})
        self.area_width = float(area.get("width", 100))
        self.area_height = float(area.get("height", 80))
        self.grid_size = float(area.get("grid", 5))

        self.devices = data.get("devices", {})
        self.links = data.get("links", [])
        self.flows = data.get("flows", [])

        self._recompute_counters()

        self.selected_device = None
        self.link_start = None
        self.info_text.clear()

        self._apply_area_to_ui()
        self.set_mode("select")
        self.redraw_network()

    def new_project(self):

        proceed = QMessageBox.question(
            self,
            "New Project",
            "This clears the current topology. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if proceed != QMessageBox.Yes:
            return

        self._reset_topology_state()

        self.area_width = 100
        self.area_height = 80
        self.grid_size = 5
        self._apply_area_to_ui()

        self.create_grid()

    def new_from_example(self):

        proceed = QMessageBox.question(
            self,
            "New from Example",
            "This clears the current topology and loads a worked example. Continue?",
            QMessageBox.Yes | QMessageBox.No,
        )
        if proceed != QMessageBox.Yes:
            return

        self._reset_topology_state()

        self.area_width = 100
        self.area_height = 60
        self.grid_size = 5
        self._apply_area_to_ui()

        clock_fields = self._default_clock_fields()

        self.devices = {
            "GM1": {
                "type": "grandmaster", "x": 10, "y": 10, "ports": 0,
                "port_speed": "1Gbps", "queues": 8, "clock_type": "802.1AS",
                **clock_fields,
                "has_time_synchronization": True,
                "has_ingress_traffic_filtering": False,
                "has_egress_traffic_shaping": False,
                "has_stream_redundancy": False,
                "has_incoming_streams": False,
                "has_outgoing_streams": False,
                "has_frame_preemption": False,
                "has_cutthrough_switching": False,
                "gptp_node_type": "MASTER_NODE",
            },
            "ES1": {
                "type": "end_system", "x": 10, "y": 40, "ports": 0,
                "port_speed": "1Gbps", "queues": 8, "clock_type": "802.1AS",
                **clock_fields,
                "has_time_synchronization": True,
                "has_ingress_traffic_filtering": False,
                "has_egress_traffic_filtering": False,
                "has_egress_traffic_shaping": False,
                "has_stream_redundancy": False,
                "has_incoming_streams": False,
                "has_outgoing_streams": True,
                "has_frame_preemption": False,
                "has_cutthrough_switching": False,
                "gptp_node_type": "SLAVE_NODE",
            },
            "SW1": {
                "type": "switch", "x": 35, "y": 40, "ports": 0,
                "port_speed": "1Gbps", "queues": 8, "clock_type": "802.1AS",
                **clock_fields,
                "has_time_synchronization": True,
                "has_ingress_traffic_filtering": True,
                "has_egress_traffic_shaping": True,
                "has_stream_redundancy": False,
                "has_incoming_streams": True,
                "has_outgoing_streams": True,
                "has_frame_preemption": False,
                "has_cutthrough_switching": False,
                "has_gptp": True,
                "gptp_node_type": "BRIDGE_NODE",
                "gptp_slave_port": "eth0",
            },
            "SW2": {
                "type": "switch", "x": 65, "y": 40, "ports": 0,
                "port_speed": "1Gbps", "queues": 8, "clock_type": "802.1AS",
                **clock_fields,
                "has_time_synchronization": True,
                "has_ingress_traffic_filtering": True,
                "has_egress_traffic_shaping": True,
                "has_stream_redundancy": False,
                "has_incoming_streams": True,
                "has_outgoing_streams": True,
                "has_frame_preemption": False,
                "has_cutthrough_switching": False,
                "has_gptp": True,
                "gptp_node_type": "BRIDGE_NODE",
                "gptp_slave_port": "eth0",
            },
            "ES2": {
                "type": "end_system", "x": 90, "y": 40, "ports": 0,
                "port_speed": "1Gbps", "queues": 8, "clock_type": "802.1AS",
                **clock_fields,
                "has_time_synchronization": True,
                "has_ingress_traffic_filtering": False,
                "has_egress_traffic_filtering": False,
                "has_egress_traffic_shaping": False,
                "has_stream_redundancy": False,
                "has_incoming_streams": True,
                "has_outgoing_streams": False,
                "has_frame_preemption": False,
                "has_cutthrough_switching": False,
                "gptp_node_type": "SLAVE_NODE",
            },
        }

        self.links = [
            {"link_id": "L1", "source": "GM1", "destination": "SW1", "bitrate": "100Mbps", "delay": "0.001", "duplex": "full", "length": "auto", "redundant": False},
            {"link_id": "L2", "source": "ES1", "destination": "SW1", "bitrate": "100Mbps", "delay": "0.001", "duplex": "full", "length": "auto", "redundant": False},
            {"link_id": "L3", "source": "SW1", "destination": "SW2", "bitrate": "100Mbps", "delay": "0.001", "duplex": "full", "length": "auto", "redundant": False},
            {"link_id": "L4", "source": "SW2", "destination": "ES2", "bitrate": "100Mbps", "delay": "0.001", "duplex": "full", "length": "auto", "redundant": False},
        ]

        self.flows = [
            {
                "flow_id": 1, "source": "ES1", "destination": "ES2",
                "traffic_type": "HRT", "pcp_queue_no": 6,
                "payload_size_kb": 100.0, "deadline_microsec": 10.0,
                "stream_periodicity_microsec": 20.0, "burst_size_mb": 15.0,
                "max_jitter_microsec": 5.0, "reliability": 0.999,
                "bandwidth_mbps": 50.0, "color": self.FLOW_COLOR_PALETTE[0],
            },
        ]

        self._recompute_counters()
        self.redraw_network()

    def open_project(self):

        path, _ = QFileDialog.getOpenFileName(
            self, "Open Project", "", "TSN topology (*.json);;All files (*)"
        )
        if not path:
            return

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "Open Project", f"Could not open that file:\n{exc}")
            return

        self._load_topology_dict(data)

    def save_project(self):

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Project", "tsn_topology.json", "TSN topology (*.json)"
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._topology_dict(), f, indent=4)
        except OSError as exc:
            QMessageBox.critical(self, "Save Project", f"Could not save:\n{exc}")
            return

        QMessageBox.information(self, "Saved", f"Topology saved to:\n{path}")

    def export_ned_ini(self):
        self.generate_ned_ini()


# =========================================================
# MAIN
# =========================================================

def main():
    app = QApplication(sys.argv)
    window = TSNDesigner()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())