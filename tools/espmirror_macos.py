# Copyright (c) 2026 Maksim Nagaev
# SPDX-License-Identifier: Apache-2.0
"""On macOS the window runs as its own app, espmirror.app, so the system asks for the microphone in
espmirror's name. A terminal's Python cannot ask: its Python.app declares no microphone use."""
import ctypes
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys

BUNDLE_ID = "io.github.menemy.espmirror"
MARK = "ESPMIRROR_APP"
INFO = {
    "CFBundleIdentifier": BUNDLE_ID,
    "CFBundleName": "espmirror",
    "CFBundleDisplayName": "espmirror",
    "CFBundleExecutable": "espmirror",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": "1",
    "NSHighResolutionCapable": True,
    "NSMicrophoneUsageDescription": "espmirror sends your microphone to the board you choose.",
}
PASSED = ("PYTHON", "ESPMIRROR", "MIRROR_", "QT_")


def executable():
    """The running binary: under a framework Python, Python.app's, whatever started it."""
    size = ctypes.c_uint32(0)
    libc = ctypes.CDLL(None)
    libc._NSGetExecutablePath(None, ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    if libc._NSGetExecutablePath(buf, ctypes.byref(size)):
        return None
    return os.path.realpath(buf.value.decode())


def declares_microphone():
    """Whether this process's app may ask for the microphone at all; Qt refuses to ask otherwise."""
    if sys.platform != "darwin":
        return True
    binary = executable()
    info = os.path.join(os.path.dirname(os.path.dirname(binary or "")), "Info.plist")
    try:
        with open(info, "rb") as f:
            return "NSMicrophoneUsageDescription" in plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False


def app_path():
    return os.path.expanduser("~/Library/Application Support/espmirror/espmirror.app")


def build(binary):
    """espmirror.app around a copy of this Python's binary, ad-hoc signed; rebuilt when either changes."""
    app = app_path()
    with open(binary, "rb") as f:
        stamp = hashlib.sha256(f.read() + plistlib.dumps(INFO)).hexdigest()
    mark = app + ".stamp"                     # outside the bundle: a file in it would break the seal
    try:
        with open(mark) as f:
            if f.read() == stamp and os.path.isdir(app):
                return app
    except OSError:
        pass
    shutil.rmtree(app, ignore_errors=True)
    os.makedirs(os.path.join(app, "Contents", "MacOS"))
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(INFO, f)
    shutil.copy2(binary, os.path.join(app, "Contents", "MacOS", "espmirror"))
    subprocess.run(["codesign", "--force", "--sign", "-", app], check=True, capture_output=True)
    with open(mark, "w") as f:
        f.write(stamp)
    return app


def stream_path(stream):
    """Where a standard stream goes (a terminal or a file), for the app to write there too."""
    try:
        import fcntl
        path = fcntl.fcntl(stream.fileno(), fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()
    except (OSError, ValueError, AttributeError):
        return None
    return path or None


def relaunch(argv):
    """Run this viewer as espmirror.app and wait for it; None to carry on in this process."""
    if (sys.platform != "darwin" or os.environ.get(MARK) or os.environ.get("QT_QPA_PLATFORM")
            or declares_microphone()):
        return None
    binary = executable()
    if not binary or "/Python.framework/" not in binary:     # a copy finds its framework by absolute path
        return None
    try:
        app = build(binary)
    except (OSError, subprocess.CalledProcessError):
        return None
    env = {k: v for k, v in os.environ.items() if k.startswith(PASSED)}
    env.update({MARK: "1", "__PYVENV_LAUNCHER__": sys.executable, "ESPMIRROR_CWD": os.getcwd()})
    cmd = ["open", "-n", "-W", "-a", app]
    for key, value in env.items():
        cmd += ["--env", "%s=%s" % (key, value)]
    for flag, stream in (("--stdout", sys.stdout), ("--stderr", sys.stderr)):
        path = stream_path(stream)
        if path:
            cmd += [flag, path]
    cmd += ["--args", os.path.abspath(sys.argv[0])] + list(argv)
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130
    except OSError:
        return None


def adopt():
    """In the app: back in the directory the command ran in, so relative paths mean the same."""
    if os.environ.get(MARK) and os.environ.get("ESPMIRROR_CWD"):
        try:
            os.chdir(os.environ["ESPMIRROR_CWD"])
        except OSError:
            pass
