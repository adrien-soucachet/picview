#!/usr/bin/env python3
"""picview — a minimal photo viewer.

Opens a picture fitted to the window, steps through the rest of the folder with
the arrow keys, and rotates in place — replacing the file on disk when you move
on, losslessly for JPEG. Zooms with the wheel, or a two-finger pinch on a touchscreen or touchpad. Follows the
desktop's light/dark theme.

Deliberately not an editor.
"""

import os
import shutil
import struct
import subprocess
import sys
import tempfile

try:
    from PyQt6.QtCore import (
        QCollator, QEvent, QFile, QLineF, QPointF, QRectF, QSettings, QSize,
        QSizeF, Qt, QTimer, pyqtSignal,
    )
    from PyQt6.QtGui import (
        QAction, QColor, QEventPoint, QGuiApplication, QImageReader,
        QImageWriter, QKeySequence, QPainter, QPalette, QPixmap, QTransform,
    )
    from PyQt6.QtWidgets import (
        QApplication, QLabel, QMainWindow, QMessageBox, QWidget,
    )
except ImportError as exc:
    # A traceback here is noise: the fix is always to install a package.
    sys.exit(
        f"picview: missing dependency '{exc.name}'\n"
        "  Debian/Ubuntu:  sudo apt install python3-pyqt6\n"
        "  pip:            pip install PyQt6"
    )

APP_NAME = "picview"

MIN_SCALE = 0.05
MAX_SCALE = 16.0
ZOOM_STEP = 1.25
# How far a notch of scrolling slides the picture, for the rare scroll
# that says how far it turned but not what that is worth in pixels.
SCROLL_PIXELS = 60
# Past this the picture is drawn unsmoothed, so zooming in shows the real
# pixels instead of an interpolated blur.
CRISP_SCALE = 2.0
TOAST_MS = 1800
# Neutral near-black behind a fullscreen picture, whatever the desktop theme:
# a bright surround washes out everything shown against it.
FULLSCREEN_BG = QColor("#101010")

_COLLATOR = None


def name_key(name):
    """Order names the way the desktop's file manager does.

    Dolphin sorts with QCollator, so borrowing the same one means picview walks
    a folder in exactly the order you were just looking at — IMG_9 before
    IMG_10, case ignored, and "été" filed under E rather than after Z the way a
    plain codepoint sort would leave it.
    """
    global _COLLATOR
    if _COLLATOR is None:
        _COLLATOR = QCollator()
        _COLLATOR.setNumericMode(True)
        _COLLATOR.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    return _COLLATOR.sortKey(name)


def supported_extensions():
    """Whatever this Qt build's image plugins can actually read."""
    return {bytes(fmt).decode("ascii").lower()
            for fmt in QImageReader.supportedImageFormats()}


def modified(entry):
    """Last-modified time, or 0 for anything that refuses to stat."""
    try:
        if isinstance(entry, os.DirEntry):
            return entry.stat().st_mtime
        return os.stat(entry).st_mtime
    except OSError:
        return 0.0


def list_pictures(folder, include=None):
    """The folder's pictures, newest first — the order Dolphin shows them in.

    `include` is kept even if Qt does not advertise its extension, so a picture
    opened by hand still sits in its proper place in the sequence.

    Timestamps come from `scandir`, which carries them along with the directory
    listing. On a network mount that is the difference between 14 ms and 120 ms
    for a five-thousand-photo folder.
    """
    when = {}
    extensions = supported_extensions()
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if os.path.splitext(entry.name)[1][1:].lower() in extensions:
                    when[entry.path] = modified(entry)
    except OSError:
        pass
    if include and include not in when and os.path.isfile(include):
        when[include] = modified(include)

    # Two stable passes: newest first, and photos sharing a second stay in
    # name order rather than whatever the filesystem happened to hand back.
    found = sorted(when, key=lambda path: name_key(os.path.basename(path)))
    found.sort(key=lambda path: when[path], reverse=True)
    return found


# --- rotating on disk ------------------------------------------------------
#
# An EXIF orientation is a mirror and a rotation applied to the stored pixels
# to get the picture the right way up. Written as T = mirror ∘ rotate, the
# user's own rotation R composes onto the front of it and collapses back into
# a single one of the same eight operations — which is exactly the set of
# transforms jpegtran can perform without decoding the image.

_ORIENTATION_OP = {
    1: (0, 0), 2: (1, 0), 3: (0, 180), 4: (1, 180),
    5: (1, 90), 6: (0, 90), 7: (1, 270), 8: (0, 270),
}
_OP_ORIENTATION = {op: value for value, op in _ORIENTATION_OP.items()}
_OP_JPEGTRAN = {
    (0, 0): [],
    (0, 90): ["-rotate", "90"],
    (0, 180): ["-rotate", "180"],
    (0, 270): ["-rotate", "270"],
    (1, 0): ["-flip", "horizontal"],
    (1, 90): ["-transpose"],
    (1, 180): ["-flip", "vertical"],
    (1, 270): ["-transverse"],
}


def compose(orientation, degrees):
    """The single operation equal to `degrees` clockwise after `orientation`."""
    mirror, rotation = _ORIENTATION_OP.get(orientation, (0, 0))
    # A mirror turns a following rotation into its opposite.
    rotation = (rotation - degrees if mirror else rotation + degrees) % 360
    return mirror, rotation


def exif_orientation_slot(data):
    """Locate the EXIF orientation: (value, byte offset, byte order) or None."""
    try:
        if not data.startswith(b"\xff\xd8"):
            return None
        at = 2
        while at + 4 <= len(data) and data[at] == 0xFF:
            marker = data[at + 1]
            if marker == 0xFF:          # fill byte before the real marker
                at += 1
                continue
            if marker in (0xD8, 0xD9, 0xDA) or 0xD0 <= marker <= 0xD7:
                return None             # image data starts here; no EXIF
            length = int.from_bytes(data[at + 2:at + 4], "big")
            if marker == 0xE1 and data[at + 4:at + 10] == b"Exif\x00\x00":
                return _orientation_in_tiff(data, at + 10, length - 8)
            at += 2 + length
    except (IndexError, struct.error):
        pass
    return None


def _orientation_in_tiff(data, base, size):
    order = data[base:base + 2]
    if order == b"II":
        end = "<"
    elif order == b"MM":
        end = ">"
    else:
        return None
    first_ifd = struct.unpack_from(end + "I", data, base + 4)[0]
    entries_at = base + first_ifd
    if not base <= entries_at < base + size:
        return None
    count = struct.unpack_from(end + "H", data, entries_at)[0]
    for i in range(count):
        entry = entries_at + 2 + i * 12
        if entry + 12 > base + size:
            break
        tag, kind = struct.unpack_from(end + "HH", data, entry)
        if tag == 0x0112 and kind == 3:     # Orientation, SHORT
            value_at = entry + 8            # fits inline, no indirection
            return struct.unpack_from(end + "H", data, value_at)[0], value_at, end
    return None


def jpeg_end(data):
    """Offset just past the primary JPEG's end-of-image marker.

    Phone photos are rarely just one JPEG. Google appends a second image after
    it — the MPF gain map on every Pixel shot, and the video of a Motion Photo
    — and that payload lives past the EOI where no JPEG tool will look for it.
    """
    try:
        at, size = 2, len(data)
        while at + 4 <= size and data[at] == 0xFF:
            marker = data[at + 1]
            if marker == 0xFF:
                at += 1
                continue
            if marker == 0xD9:
                return at + 2
            if marker == 0xDA:              # entropy-coded scan: scan for EOI
                found = data.find(b"\xff\xd9", at + 2)
                return size if found == -1 else found + 2
            at += 2 + int.from_bytes(data[at + 2:at + 4], "big")
    except (IndexError, ValueError):
        pass
    return len(data)


def with_orientation(data, value):
    """Return `data` with its EXIF orientation set to `value`, if it has one."""
    slot = exif_orientation_slot(data)
    if slot is None:
        return data
    _, value_at, end = slot
    patched = bytearray(data)
    struct.pack_into(end + "H", patched, value_at, value)
    return bytes(patched)


def _replace(path, temp):
    """Move `temp` over `path`, keeping the original's mode and timestamps.

    The timestamps matter: a folder sorted by date should not reshuffle just
    because a picture was turned the right way up.
    """
    try:
        before = os.stat(path)
        os.chmod(temp, before.st_mode & 0o7777)
        os.replace(temp, path)
        os.utime(path, (before.st_atime, before.st_mtime))
    except OSError as exc:
        _discard(temp)
        return str(exc)
    return None


def _discard(temp):
    try:
        os.unlink(temp)
    except OSError:
        pass


def _temp_beside(path, suffix=""):
    handle, temp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".", prefix=".picview-", suffix=suffix
    )
    os.close(handle)
    return temp


def _write_bytes(path, data):
    temp = _temp_beside(path)
    try:
        with open(temp, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    except OSError as exc:
        _discard(temp)
        return str(exc)
    return _replace(path, temp)


def _jpegtran(path, args):
    try:
        done = subprocess.run(
            ["jpegtran", *args, "-copy", "all", path], capture_output=True
        )
    except OSError:
        return None
    return done.stdout if done.returncode == 0 and done.stdout else None


def _rotate_jpeg(path, degrees):
    try:
        with open(path, "rb") as source:
            data = source.read()
    except OSError as exc:
        return str(exc)

    slot = exif_orientation_slot(data)
    operation = compose(slot[0] if slot else 1, degrees)
    transform = _OP_JPEGTRAN[operation]

    if jpeg_end(data) < len(data):
        # There is a second image or a Motion Photo video attached past the
        # end of this JPEG. jpegtran would drop it, and even re-appending it
        # would break the MPF offsets that point at it, since those are
        # counted from the start of the file. Turning the picture by its
        # orientation tag moves nothing: two bytes change and the payload
        # survives untouched.
        if slot:
            return _write_bytes(
                path, with_orientation(data, _OP_ORIENTATION[operation])
            )
        return "cannot rotate without discarding the data attached to this photo"

    # Best case: jpegtran shuffles the DCT blocks around without ever decoding
    # the image, so not one pixel changes. Then the orientation tag it copied
    # across is stale and has to go back to 1.
    output = _jpegtran(path, transform + ["-perfect"])
    if output is not None:
        return _write_bytes(path, with_orientation(output, 1))

    # -perfect refused: the width or height is not a whole number of blocks,
    # so the edge cannot be turned cleanly. If the file carries an orientation
    # tag, rewriting just that tag is still exact.
    if slot:
        return _write_bytes(path, with_orientation(data, _OP_ORIENTATION[operation]))

    output = _jpegtran(path, transform)
    if output is None:
        if shutil.which("jpegtran") is None:
            return "rotating a JPEG needs jpegtran (apt install libjpeg-turbo-progs)"
        return "jpegtran could not rotate this file"
    return _write_bytes(path, output)


def _rotate_other(path, degrees):
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        return reader.errorString() or "cannot read this picture"
    # A quarter turn is exact, so nothing is resampled either way.
    image = image.transformed(
        QTransform().rotate(degrees), Qt.TransformationMode.FastTransformation
    )
    temp = _temp_beside(path, os.path.splitext(path)[1])
    writer = QImageWriter(temp)
    if not writer.canWrite() or not writer.write(image):
        error = writer.errorString() or "cannot write this format"
        _discard(temp)
        return error
    return _replace(path, temp)


def rotate_file(path, degrees):
    """Turn the picture on disk. Returns None, or a message explaining why not."""
    if not os.access(path, os.W_OK):
        return "read-only file"
    if os.path.splitext(path)[1].lower() in (".jpg", ".jpeg", ".jpe"):
        return _rotate_jpeg(path, degrees)
    return _rotate_other(path, degrees)


# --- deleting --------------------------------------------------------------

def delete_file(path):
    """Move the picture to the Trash. Returns None, or why it could not.

    Qt follows the freedesktop trash spec, so the file lands exactly where
    Dolphin's own delete puts it and can be put back from there. A removable
    disk or a network mount without a trash directory of its own will refuse;
    falling back to unlink() there would quietly turn a reversible delete into
    a permanent one, so the refusal is reported instead.
    """
    handle = QFile(path)
    if handle.moveToTrash():
        return None
    return handle.errorString() or "cannot move this file to the Trash"


# --- widgets ---------------------------------------------------------------

class Toast(QLabel):
    """A short-lived message over the picture."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        parent.installEventFilter(self)

    def say(self, text, error=False):
        background = "rgba(150,32,32,220)" if error else "rgba(0,0,0,190)"
        self.setStyleSheet(
            f"background-color: {background}; color: #ffffff;"
            "padding: 7px 12px; border-radius: 6px;"
        )
        self.setText(text)
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        self._timer.start(TOAST_MS)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Resize:
            self._place()
        return False

    def _place(self):
        self.move(18, self.parent().height() - self.height() - 18)


class Canvas(QWidget):
    """Draws one picture: fitted by default, free zoom, drag to pan."""

    scale_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._pixmap = QPixmap()
        self._scaled = QPixmap()
        self._scaled_for = None
        self._scale = 1.0
        self._fitting = True
        self._origin = QPointF(0, 0)    # top-left of the picture, widget coords
        self._drag_from = None
        self._background = QColor(Qt.GlobalColor.darkGray)
        self._touch_window = None
        self._pinching = False
        self._pinch_span = 0.0          # how far apart the fingers started
        self._pinch_scale = 1.0         # the zoom they started from
        self._pinch_centre = QPointF(0, 0)
        self._pad_factor = None         # how far a touchpad pinch has opened
        self._pad_scale = 1.0           # the zoom it started from

    # --- content -----------------------------------------------------------

    def set_pixmap(self, pixmap):
        self._pixmap = pixmap
        self._scaled_for = None
        self.fit()

    def rotate(self, degrees):
        """Turn the picture on screen only — nothing is written to disk here.

        A quarter turn moves whole pixels around, so this preview is exactly
        the picture the file will hold once the rotation is saved.
        """
        if self.has_picture():
            self.set_pixmap(self._pixmap.transformed(
                QTransform().rotate(degrees), Qt.TransformationMode.FastTransformation
            ))

    def has_picture(self):
        return not self._pixmap.isNull()

    def picture(self):
        """The picture at full size, as it looks on screen."""
        return self._pixmap

    def picture_size(self):
        return self._pixmap.size() if self.has_picture() else QSize()

    def set_background(self, color):
        self._background = color
        self.update()

    # --- zoom --------------------------------------------------------------

    @property
    def scale(self):
        return self._scale

    def is_fitting(self):
        return self._fitting

    def _ratio(self):
        """Screen pixels per layout pixel — 1.9 on a 190%-scaled desktop."""
        return self.devicePixelRatioF() or 1.0

    def _span(self):
        """Size of the picture on screen, in the layout units mouse events use."""
        drawn = self._scale / self._ratio()
        return self._pixmap.width() * drawn, self._pixmap.height() * drawn

    def fit_scale(self):
        if not self.has_picture():
            return 1.0
        ratio = self._ratio()
        wide = self.width() * ratio / self._pixmap.width()
        tall = self.height() * ratio / self._pixmap.height()
        # Shrink to fit, but never blow a small picture up unasked.
        return min(wide, tall, 1.0)

    def fit(self):
        self._fitting = True
        self._rescale(self.fit_scale(), None)

    def actual_size(self, anchor=None):
        self._fitting = False
        self._rescale(1.0, anchor)

    def zoom_by(self, factor, anchor=None):
        self._fitting = False
        self._rescale(self._scale * factor, anchor)

    def _rescale(self, scale, anchor):
        scale = max(MIN_SCALE, min(MAX_SCALE, scale))
        ratio = self._ratio()
        if anchor is None:
            anchor = QPointF(self.width() / 2, self.height() / 2)
        # Whatever sits under `anchor` stays under it.
        fixed = (anchor - self._origin) / (self._scale / ratio)
        self._origin = anchor - fixed * (scale / ratio)
        self._scale = scale
        self._clamp()
        self.update()
        self.scale_changed.emit()

    def _clamp(self):
        """Keep the picture centred, or its edges inside the window."""
        if not self.has_picture():
            return
        width, height = self._span()
        self._origin = QPointF(
            self._axis(self._origin.x(), width, self.width()),
            self._axis(self._origin.y(), height, self.height()),
        )

    @staticmethod
    def _axis(position, span, available):
        if span <= available:
            return (available - span) / 2
        return max(min(position, 0.0), available - span)

    def _pannable(self):
        width, height = self._span()
        return width > self.width() or height > self.height()

    # --- painting ----------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background)
        if not self.has_picture():
            return
        width, height = self._span()
        if self._scale <= 1.0:
            # Shrink once, in *screen* pixels, and hand Qt a pixmap that says
            # what it is. Shrinking to layout pixels instead and letting the
            # compositor stretch the result back up is the difference between
            # a sharp photo and a soft one on a scaled desktop.
            ratio = self._ratio()
            target = QSize(max(1, round(width * ratio)),
                           max(1, round(height * ratio)))
            painter.drawPixmap(
                QPointF(round(self._origin.x()), round(self._origin.y())),
                self._downscaled(target, ratio),
            )
        else:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                                  self._scale < CRISP_SCALE)
            painter.drawPixmap(QRectF(self._origin, QSizeF(width, height)),
                               self._pixmap, QRectF(self._pixmap.rect()))

    def _downscaled(self, size, ratio):
        """The picture pre-shrunk to `size` screen pixels, cached.

        Rescaling a 12-megapixel photo on every repaint is the one thing that
        makes panning and resizing feel sluggish, so it is done once per size.
        """
        if self._scaled_for != (size, ratio):
            self._scaled = self._pixmap.scaled(
                size, Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._scaled.setDevicePixelRatio(ratio)
            self._scaled_for = (size, ratio)
        return self._scaled

    def event(self, event):
        # Dragged onto a screen with different scaling: the cache is stale and
        # a fitted picture needs remeasuring.
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self._scaled_for = None
            if self._fitting:
                self._rescale(self.fit_scale(), None)
        if event.type() == QEvent.Type.Show:
            self._watch_touches()
        if event.type() == QEvent.Type.NativeGesture and self._pad_pinch(event):
            return True
        return super().event(event)

    # --- touch -------------------------------------------------------------
    #
    # Two fingers zoom. Qt has a pinch recognizer, but its scale factor
    # measures each finger against a position that goes stale whenever the
    # *other* finger was the one that moved, so a two-finger spread ends up
    # zooming several times further than the fingers travelled. Reading the
    # touch points and dividing one distance by another is both correct and
    # less code.
    #
    # The catch is that a widget which accepts touch events stops being sent a
    # synthesized mouse — and dragging to pan and double-tapping for 1:1 are
    # both that mouse. So the touches are read one level up, off the window,
    # and passed straight on untouched.

    def _watch_touches(self):
        """Start listening in on the window this widget now belongs to."""
        window = self.window().windowHandle()
        if window is self._touch_window:
            return
        if self._touch_window is not None:
            self._touch_window.removeEventFilter(self)
        if window is not None:
            window.installEventFilter(self)
        self._touch_window = window

    def eventFilter(self, _window, event):
        kind = event.type()
        if kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate):
            self._touched([point for point in event.points()
                           if point.state() != QEventPoint.State.Released])
        elif kind in (QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            self._touched([])
        return False    # never swallow one: the mouse is synthesized from it

    def _touched(self, points):
        """Zoom about the midpoint of exactly two fingers, and follow them.

        Moving the pair together slides the picture as well, so a pinch that
        drifts across the panel zooms and pans in the one motion.
        """
        if len(points) != 2 or not self.has_picture():
            self._pinching = False
            return
        first, second = (self._local(point.position()) for point in points)
        span = QLineF(first, second).length()
        centre = (first + second) / 2
        if not self._pinching or self._pinch_span < 1:
            # The opening frame — or two fingers landed on the same spot, and
            # there is no distance yet to measure the next one against.
            self._pinching = True
            self._pinch_span = span
            self._pinch_scale = self._scale
            self._pinch_centre = centre
            # The first finger down already looks like a mouse press; the
            # second one arriving means it was never a drag.
            self._drag_from = None
            self.unsetCursor()
            return
        self._origin += centre - self._pinch_centre
        self._pinch_centre = centre
        self._fitting = False
        # Measured against where the fingers started rather than the previous
        # frame: a repeated frame then changes nothing, and after the zoom has
        # run up against a limit it comes back down the moment the fingers do,
        # instead of having to unwind an accumulated overshoot first.
        self._rescale(self._pinch_scale * span / self._pinch_span, centre)

    # --- touchpad ----------------------------------------------------------
    #
    # A touchpad pinch never reaches the widget as touch: the desktop reads the
    # fingers itself and hands the application the gesture, already measured.

    def _pad_pinch(self, event):
        """Follow a pinch on the touchpad. True if the event was ours."""
        # Zoom is the only one of these picview wants; the swipes and
        # rotations belong to whatever else might be listening.
        kind = event.gestureType()
        if kind == Qt.NativeGestureType.BeginNativeGesture:
            self._begin_pad_pinch()
            return False
        if kind == Qt.NativeGestureType.EndNativeGesture:
            self._pad_factor = None
            return False
        if kind != Qt.NativeGestureType.ZoomNativeGesture or not self.has_picture():
            return False
        if self._pad_factor is None:
            self._begin_pad_pinch()
        # Each value is a step of how far the fingers have opened *since the
        # pinch began*, not a factor to multiply by: adding them up gives the
        # spread, whereas multiplying them compounds into several times the
        # zoom the fingers asked for.
        self._pad_factor = max(self._pad_factor + event.value(), MIN_SCALE)
        self._fitting = False
        # The position on one of these is measured from the window, not from
        # this widget.
        self._rescale(self._pad_scale * self._pad_factor,
                      self._local(event.position()))
        return True

    def _begin_pad_pinch(self):
        self._pad_factor = 1.0
        self._pad_scale = self._scale

    def _local(self, point):
        """A position measured from the window, in the coordinates zoom uses."""
        return QPointF(self.mapFrom(self.window(), point.toPoint()))

    # --- mouse -------------------------------------------------------------

    def mouseDoubleClickEvent(self, event):
        # 1:1 under the cursor, and back to fit.
        if self._fitting:
            self.actual_size(event.position())
        else:
            self.fit()

    def mousePressEvent(self, event):
        if self._pinching:
            return
        if event.button() == Qt.MouseButton.LeftButton and self._pannable():
            self._drag_from = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self._drag_from is None or self._pinching:
            return
        self._origin += event.position() - self._drag_from
        self._drag_from = event.position()
        self._clamp()
        self.update()

    def mouseReleaseEvent(self, _event):
        self._drag_from = None
        self.unsetCursor()

    def wheelEvent(self, event):
        """A wheel zooms; a touchpad's two fingers scroll the picture instead.

        The two arrive as the same event, and the device it claims to come from
        is no help — a plain wheel mouse reports itself as a touchpad here. The
        scroll phase does tell them apart: a touchpad brackets its scrolling
        with a begin and an end, and a wheel has no phase at all.
        """
        if not self.has_picture():
            return
        if event.phase() != Qt.ScrollPhase.NoScrollPhase:
            self._scroll(event)
            event.accept()
            return
        steps = event.angleDelta().y() / 120
        if not steps:
            return
        self.zoom_by(ZOOM_STEP ** steps, event.position())
        event.accept()

    def _scroll(self, event):
        """Slide the picture under a touchpad scroll, the way a drag would."""
        delta = QPointF(event.pixelDelta())
        if delta.isNull():
            # No pixel figure offered — a notch's worth of angle instead. The
            # events that open and close a scroll carry neither, and adding
            # nothing at all is exactly right for those.
            delta = QPointF(event.angleDelta()) * SCROLL_PIXELS / 120
        # Qt has already applied whichever scrolling direction the desktop is
        # set to, so following the delta follows the setting.
        self._origin += delta
        self._clamp()
        self.update()

    def resizeEvent(self, event):
        if self._fitting:
            self._rescale(self.fit_scale(), None)
        else:
            self._clamp()
        super().resizeEvent(event)


class Viewer(QMainWindow):
    def __init__(self, path):
        super().__init__()
        self.settings = QSettings(APP_NAME, APP_NAME)
        self.files = []
        self.index = 0
        self._pending = 0           # rotation shown but not yet written
        self._was_maximized = False

        self.canvas = Canvas()
        self.canvas.scale_changed.connect(self._update_title)
        self.setCentralWidget(self.canvas)
        self.toast = Toast(self.canvas)

        self._scan(path)
        self._show()
        self._install_shortcuts()
        self._apply_theme()

        # Re-theme when the desktop switches between light and dark.
        QApplication.instance().styleHints().colorSchemeChanged.connect(
            lambda _scheme: self._apply_theme()
        )
        self._restore_geometry()

    # --- the folder --------------------------------------------------------

    def current(self):
        return self.files[self.index] if self.files else None

    def _scan(self, path):
        """List the folder's pictures, keeping `path` selected."""
        files = list_pictures(os.path.dirname(path) or ".", include=path)
        self.files = files or [path]
        self.index = self.files.index(path) if path in self.files else 0

    def _show(self):
        path = self.current()
        if path is None:            # the last picture was just deleted
            self.canvas.set_pixmap(QPixmap())
            self._update_title()
            return
        reader = QImageReader(path)
        reader.setAutoTransform(True)       # honour the EXIF orientation
        image = reader.read()
        if image.isNull():
            self.canvas.set_pixmap(QPixmap())
            self.toast.say(
                f"{os.path.basename(path)}: {reader.errorString() or 'cannot open'}",
                error=True,
            )
        else:
            self.canvas.set_pixmap(QPixmap.fromImage(image))
        self._update_title()

    def _step(self, delta):
        if len(self.files) < 2:
            return
        failed = self._leave()
        self.index = (self.index + delta) % len(self.files)
        self._show()
        self._announce(failed)

    def _go(self, index):
        if not self.files:
            return
        index = max(0, min(index, len(self.files) - 1))
        if index == self.index:     # Home on the first photo: nothing to leave
            return
        failed = self._leave()
        self.index = index
        self._show()
        self._announce(failed)

    def _leave(self):
        """Save the picture being stepped off. Returns a message, or None.

        The message names the file, because by the time it is on screen the
        photo it is about is not.
        """
        name = os.path.basename(self.current() or "")
        error = self._flush()
        return f"{name}: {error}" if error else None

    def _announce(self, failed):
        if failed:
            self.toast.say(failed, error=True)
        elif self.isFullScreen() and self.current():
            # No title bar up there to read the name off.
            self.toast.say(os.path.basename(self.current()))

    def _rotate(self, degrees):
        """Turn the picture on screen. The file is rewritten later, by _flush.

        Rotating is usually a burst of keypresses on the way to the right way
        up, and each one would otherwise rewrite the file — four of them for a
        full turn that changes nothing. Holding the angle until the picture
        leaves the screen means one write, or none.
        """
        path = self.current()
        if not path or not self.canvas.has_picture():
            return
        if not os.access(path, os.W_OK):
            # Say so now rather than at the far end of the folder.
            self.toast.say("read-only file", error=True)
            return
        self._pending = (self._pending + degrees) % 360
        self.canvas.rotate(degrees)
        self.toast.say("rotated — saved when you move on")

    def _flush(self):
        """Write any held rotation to disk. Returns None, or why it could not."""
        degrees, self._pending = self._pending, 0
        path = self.current()
        if not degrees or not path:
            return None
        return rotate_file(path, degrees)

    def _delete(self):
        """Trash the picture on screen, once, after asking."""
        path = self.current()
        if not path or not self._confirm_delete(path):
            return
        error = delete_file(path)
        if error:
            self.toast.say(error, error=True)
            return
        self._pending = 0           # no sense rewriting a file on its way out
        del self.files[self.index]
        # The photo that was next slides into this slot; at the end, wrap the
        # way Right does rather than stopping.
        if self.index >= len(self.files):
            self.index = 0
        self._show()
        self.toast.say(
            f"{os.path.basename(path)} — moved to Trash" if self.files
            else "no pictures left in this folder"
        )

    def _confirm_delete(self, path):
        """Ask first. Cancel is the default: nothing in picview undoes this."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(f"Delete — {APP_NAME}")
        box.setText(f"Move “{os.path.basename(path)}” to the Trash?")
        box.setInformativeText("You can put it back from your file manager.")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText("Move to Trash")
        box.button(QMessageBox.StandardButton.No).setText("Cancel")
        return box.exec() == QMessageBox.StandardButton.Yes

    def _copy_path(self):
        """Put the path of the photo on screen on the clipboard."""
        path = self.current()
        if not path:
            return
        QGuiApplication.clipboard().setText(path)
        self.toast.say("path copied")

    def _copy_image(self):
        """Put the photo itself on the clipboard, to paste into another app.

        This is the picture on screen, so a rotation still waiting to be
        written is part of what gets copied.
        """
        if not self.canvas.has_picture():
            return
        QGuiApplication.clipboard().setImage(self.canvas.picture().toImage())
        self.toast.say("photo copied")

    def _update_title(self):
        path = self.current()
        if not path:
            self.setWindowTitle(APP_NAME)
            return
        parts = [os.path.basename(path)]
        if len(self.files) > 1:
            parts.append(f"{self.index + 1}/{len(self.files)}")
        size = self.canvas.picture_size()
        if not size.isEmpty():
            parts.append(f"{size.width()}×{size.height()}")
            parts.append(f"{round(self.canvas.scale * 100)}%")
        self.setWindowTitle(" · ".join(parts) + f" — {APP_NAME}")

    # --- window ------------------------------------------------------------

    def _apply_theme(self):
        window = QApplication.instance().palette().color(QPalette.ColorRole.Window)
        self.canvas.set_background(
            FULLSCREEN_BG if self.isFullScreen() else window
        )

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showMaximized() if self._was_maximized else self.showNormal()
        else:
            self._was_maximized = self.isMaximized()
            self.showFullScreen()
        self._apply_theme()

    def _escape(self):
        if self.isFullScreen():
            self._toggle_fullscreen()
        else:
            self.close()

    def _restore_geometry(self):
        """Reopen at the previous size and maximised/fullscreen state.

        Wayland does not let a client place its own window, so the position is
        the compositor's to decide; size and window state do restore.
        """
        saved = self.settings.value("geometry")
        if saved is None or not self.restoreGeometry(saved):
            self.resize(1100, 800)
        self._was_maximized = self.settings.value("maximized", False, type=bool)
        if self.settings.value("fullscreen", False, type=bool):
            self.showFullScreen()
            self._apply_theme()
        elif self._was_maximized:
            self.showMaximized()

    def closeEvent(self, event):
        name = os.path.basename(self.current() or "")
        error = self._flush()
        if error:
            # A toast on a window that is closing is never seen, and a lost
            # rotation should not go unmentioned. The close still goes ahead:
            # holding the window open would not fix whatever refused the write.
            QMessageBox.warning(
                self, f"Not saved — {APP_NAME}",
                f"“{name}” could not be saved rotated:\n{error}",
            )

        # Store the normal-state geometry, so unmaximising later lands on the
        # last floating size rather than the screen-sized one.
        self.settings.setValue("fullscreen", self.isFullScreen())
        self.settings.setValue("maximized", self.isMaximized() or self._was_maximized)
        if not self.isFullScreen() and not self.isMaximized():
            self.settings.setValue("geometry", self.saveGeometry())
        self.settings.sync()
        super().closeEvent(event)

    def _install_shortcuts(self):
        def add(sequence, slot):
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            self.addAction(action)

        add("Right", lambda: self._step(1))
        add("Left", lambda: self._step(-1))
        add("Space", lambda: self._step(1))
        add("Backspace", lambda: self._step(-1))
        add("Home", lambda: self._go(0))
        add("End", lambda: self._go(len(self.files) - 1))

        add("F", self._toggle_fullscreen)
        add("F12", self._toggle_fullscreen)
        add("F11", self._toggle_fullscreen)
        add("Escape", self._escape)

        add("R", lambda: self._rotate(90))
        add("Ctrl+R", lambda: self._rotate(90))
        add("L", lambda: self._rotate(-90))
        add("Ctrl+Shift+R", lambda: self._rotate(-90))

        add("Delete", self._delete)

        add(QKeySequence.StandardKey.Copy, self._copy_path)
        add("Ctrl+Shift+C", self._copy_image)

        add("0", self.canvas.fit)
        add("Ctrl+0", self.canvas.fit)
        add("1", lambda: self.canvas.actual_size())
        add("Ctrl+1", lambda: self.canvas.actual_size())
        add(QKeySequence.StandardKey.ZoomIn, lambda: self.canvas.zoom_by(ZOOM_STEP))
        add("+", lambda: self.canvas.zoom_by(ZOOM_STEP))
        add("=", lambda: self.canvas.zoom_by(ZOOM_STEP))
        add(QKeySequence.StandardKey.ZoomOut, lambda: self.canvas.zoom_by(1 / ZOOM_STEP))
        add("-", lambda: self.canvas.zoom_by(1 / ZOOM_STEP))

        add(QKeySequence.StandardKey.Close, self.close)
        add("Ctrl+Q", self.close)


def main():
    usage = f"usage: {APP_NAME} PHOTO|FOLDER"
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(usage)
        return 0
    if not args:
        print(usage, file=sys.stderr)
        return 2
    if len(args) > 1:
        # Nearly always an unquoted path with a space in it.
        print(f"{usage}\n{APP_NAME}: expected one path, got {len(args)} "
              "arguments — quote the path if it contains spaces", file=sys.stderr)
        return 2

    QGuiApplication.setDesktopFileName(APP_NAME)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    # Qt refuses any picture whose decoded pixels pass 256 MB, a guard against
    # decompression bombs arriving from the web. Nothing arrives here: picview
    # decodes the file you pointed it at, and a full-page browser capture is
    # routinely taller than that cap allows.
    QImageReader.setAllocationLimit(0)

    # The image plugins are only loaded once QApplication exists, so the list
    # of readable formats is not trustworthy before this point.
    target = os.path.abspath(args[0])
    if os.path.isdir(target):
        pictures = list_pictures(target)
        if not pictures:
            print(f"{APP_NAME}: no pictures in {target}", file=sys.stderr)
            return 1
        target = pictures[0]
    elif not os.path.isfile(target):
        print(f"{APP_NAME}: no such file: {args[0]}", file=sys.stderr)
        return 1

    viewer = Viewer(target)
    viewer.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
