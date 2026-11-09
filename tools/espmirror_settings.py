# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
"""Viewer preferences. Qt is imported only when the window needs it."""
import math
import os


DEFAULTS = dict(scale=1.0, on_top=False, show_touches=True, sound=False,
                remember_position=True, screenshots="", recordings="", log="",
                usb=True, wifi=False, hosts="")


def settings_store():
    from PySide6.QtCore import QSettings
    return QSettings("espmirror", "mirror")


def read_preferences(settings=None):
    settings = settings if settings is not None else settings_store()
    values = {}
    for key, default in DEFAULTS.items():
        try:
            values[key] = settings.value(key, default, type=type(default))
        except (TypeError, ValueError, OverflowError):
            values[key] = default
    if not math.isfinite(values["scale"]) or not .25 <= values["scale"] <= 8:
        values["scale"] = DEFAULTS["scale"]
    return values


def validate_hosts(text):
    for target in text.split():
        if target.count(":") == 1:
            host, port = target.rsplit(":", 1)
            if not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
                raise ValueError("Use a host or host:port, with a port from 1 to 65535.")
    return text.strip()


def edit_preferences(parent=None, settings=None):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                                  QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                  QMessageBox, QPlainTextEdit, QPushButton, QTabWidget,
                                  QVBoxLayout, QWidget)

    settings = settings if settings is not None else settings_store()
    values = read_preferences(settings)
    dialog = QDialog(parent)
    dialog.setObjectName("viewer_settings")
    dialog.setWindowTitle("espmirror Settings")
    dialog.setMinimumWidth(480)
    layout = QVBoxLayout(dialog)
    tabs = QTabWidget()
    layout.addWidget(tabs)
    fields = {}

    def page(title):
        widget = QWidget()
        form = QFormLayout(widget)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        tabs.addTab(widget, title)
        return form

    def checkbox(form, key, label):
        field = QCheckBox(label)
        field.setObjectName(key)
        field.setChecked(values[key])
        fields[key] = field
        form.addRow(field)

    def path_field(form, key, label, file=False):
        row = QHBoxLayout()
        field = QLineEdit(values[key])
        field.setObjectName(key)
        field.setPlaceholderText("Disabled" if file else "Desktop (home folder if unavailable)")
        fields[key] = field
        button = QPushButton("Browse…")
        button.setAccessibleName("Browse for " + label.lower())
        field_label = QLabel(label)
        field_label.setBuddy(field)

        def browse():
            start = os.path.expanduser(field.text()) or os.path.expanduser("~")
            if file:
                path, _ = QFileDialog.getSaveFileName(dialog, label, start, "Log files (*.log);;All files (*)",
                                                     options=QFileDialog.Option.DontConfirmOverwrite)
            else:
                path = QFileDialog.getExistingDirectory(dialog, label, start)
            if path:
                field.setText(path)

        button.clicked.connect(browse)
        row.addWidget(field)
        row.addWidget(button)
        form.addRow(field_label, row)

    appearance = page("Window")
    zoom = QDoubleSpinBox()
    zoom.setObjectName("scale")
    zoom.setRange(25, 800)
    zoom.setDecimals(0)
    zoom.setSingleStep(25)
    zoom.setSuffix(" %")
    zoom.setValue(values["scale"] * 100)
    fields["scale"] = zoom
    appearance.addRow("Default size", zoom)
    checkbox(appearance, "on_top", "Float on Top")
    checkbox(appearance, "show_touches", "Show touches")
    checkbox(appearance, "remember_position", "Remember window position")
    checkbox(appearance, "sound", "Listen to the first board's speaker at startup")
    hint = QLabel("Window changes apply when you save.\n"
                  "Startup sound applies the next time you open the viewer.")
    hint.setWordWrap(True)
    appearance.addRow(hint)

    files = page("Files")
    path_field(files, "screenshots", "Screenshots")
    path_field(files, "recordings", "Recordings")
    path_field(files, "log", "Console log", file=True)
    hint = QLabel("Leave capture folders empty to use Desktop.\n"
                  "The log is appended at the next launch; leave it empty to disable.")
    hint.setWordWrap(True)
    files.addRow(hint)

    connections = page("Connections")
    checkbox(connections, "usb", "Discover USB boards")
    checkbox(connections, "wifi", "Discover network boards (mDNS)")
    hosts = QPlainTextEdit(values["hosts"])
    hosts.setObjectName("hosts")
    hosts.setTabChangesFocus(True)
    hosts.setPlaceholderText("192.168.7.1\n192.168.1.42:7331\n/dev/ttyACM0 or COM7")
    hosts.setMaximumHeight(120)
    fields["hosts"] = hosts
    connections.addRow("Explicit targets", hosts)
    hint = QLabel("One address or serial port per line. Applied at the next launch.\n"
                  "Targets passed on the command line replace discovery and this list.")
    hint.setWordWrap(True)
    connections.addRow(hint)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
    layout.addWidget(buttons)

    def save():
        updated = {}
        for key, field in fields.items():
            if isinstance(field, QCheckBox):
                updated[key] = field.isChecked()
            elif key == "scale":
                updated[key] = field.value() / 100
            elif key == "hosts":
                updated[key] = field.toPlainText().strip()
            else:
                text = field.text().strip()
                updated[key] = os.path.abspath(os.path.expanduser(text)) if text else ""
        try:
            validate_hosts(updated["hosts"])
            for key in ("screenshots", "recordings"):
                if updated[key] and not os.path.isdir(updated[key]):
                    raise ValueError("Choose an existing folder for %s." % key)
            if updated["log"] and (os.path.isdir(updated["log"]) or
                                   not os.path.isdir(os.path.dirname(updated["log"]))):
                raise ValueError("Choose a log file in an existing folder.")
        except ValueError as error:
            QMessageBox.warning(dialog, "Check settings", str(error))
            return
        changed = list(updated) + (["position"] if not updated["remember_position"] else [])
        previous = {key: settings.value(key) for key in changed if settings.contains(key)}
        for key, value in updated.items():
            settings.setValue(key, value)
        if not updated["remember_position"]:
            settings.remove("position")
        settings.sync()
        if settings.status() != QSettings.Status.NoError:
            for key in changed:
                if key in previous:
                    settings.setValue(key, previous[key])
                else:
                    settings.remove(key)
            settings.sync()
            buttons.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
            QMessageBox.warning(dialog, "Settings not saved",
                                "Cannot save preferences.\n"
                                "Fix access to the preferences file, then reopen the viewer.")
            return
        dialog.accept()

    buttons.accepted.connect(save)
    buttons.rejected.connect(dialog.reject)
    return dialog.exec() == QDialog.DialogCode.Accepted
