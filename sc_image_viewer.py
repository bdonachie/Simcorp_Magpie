"""
Full-size viewer for a screenshot attached to a case.
=====================================================

The thread shows attachments scaled down so one screenshot cannot fill the
pane. Double-clicking one opens it here, where it can be zoomed with the mouse
wheel and dragged around.

It is fast because the file is already on disk: the thread download saved it
before rendering, so opening is a decode rather than a fetch. Decoded source
images are kept in a small cache, so opening the same screenshot twice skips
even that.

The window paints "Loading..." first and decodes on the next event-loop tick.
Without that the window would appear only after the decode, which on a large
screenshot looks like the app has frozen.
"""

from __future__ import annotations

import os
from collections import OrderedDict

import tkinter as tk
from tkinter import ttk

try:
    from PIL import Image, ImageTk

    IMAGE_SUPPORT_AVAILABLE = True
except ImportError:
    IMAGE_SUPPORT_AVAILABLE = False

#: Decoded source images, newest last. Small, because these are full-size
#: screenshots and each one costs real memory.
_SOURCE_CACHE: "OrderedDict[str, object]" = OrderedDict()
SOURCE_CACHE_SIZE = 6

#: Leave room for the title bar, taskbar and the window's own controls.
SCREEN_MARGIN_FRACTION = 0.88

#: One wheel notch. 1.25 is a large enough step to feel responsive without
#: overshooting the detail someone is trying to read.
ZOOM_STEP = 1.25
MINIMUM_ZOOM = 0.05
MAXIMUM_ZOOM = 8.0

#: A hard ceiling on the resampled bitmap. 8x on a large screenshot would
#: otherwise allocate hundreds of megabytes: a 2560x1440 capture at 8x is
#: 236 million pixels, roughly 700 MB as RGB. The zoom is capped so the
#: viewer cannot take the app down with it.
MAXIMUM_RENDERED_PIXELS = 40_000_000

#: Rapid wheel spins would otherwise resample once per notch, which is far too
#: slow on a big screenshot. The scale updates immediately; the redraw waits.
REDRAW_DELAY_MILLISECONDS = 60


def _cached_source(image_path: str):
    """The decoded image, from cache when it has been opened before."""
    if image_path in _SOURCE_CACHE:
        _SOURCE_CACHE.move_to_end(image_path)
        return _SOURCE_CACHE[image_path]

    with Image.open(image_path) as picture:
        picture.load()
        source = picture.convert("RGB")
    _SOURCE_CACHE[image_path] = source
    _SOURCE_CACHE.move_to_end(image_path)
    while len(_SOURCE_CACHE) > SOURCE_CACHE_SIZE:
        _SOURCE_CACHE.popitem(last=False)
    return source


def show_image(parent, image_path: str, title: str = "", icon_path: str = "") -> None:
    """Open one image in its own zoomable window."""
    if not image_path or not os.path.exists(image_path) or not IMAGE_SUPPORT_AVAILABLE:
        return

    window = tk.Toplevel(parent)
    window.title(title or os.path.basename(image_path))
    window.transient(parent)
    if icon_path and os.path.exists(icon_path):
        try:
            icon_image = tk.PhotoImage(file=icon_path)
            window.iconphoto(False, icon_image)
            window._icon_image = icon_image  # noqa: SLF001 - Tk keeps no reference
        except tk.TclError:
            pass

    loading_label = ttk.Label(window, text="Loading...", padding=40, anchor="center")
    loading_label.pack(fill="both", expand=True)
    window.geometry("360x140")
    window.update_idletasks()
    window.bind("<Escape>", lambda event: window.destroy())

    def load() -> None:
        try:
            source = _cached_source(image_path)
        except (OSError, ValueError) as error:
            loading_label.config(text=f"This image could not be opened:\n{error}")
            return
        loading_label.destroy()
        ImageWindow(window, source, image_path)

    # Let "Loading..." paint before the decode blocks the event loop.
    window.after(20, load)


class ImageWindow:
    """The picture, on a canvas, with wheel zoom and drag panning."""

    def __init__(self, window, source, image_path: str):
        self.window = window
        self.source = source
        self.image_path = image_path
        self.scale = 1.0
        self.photo = None
        self._pending_redraw = None

        self.canvas = tk.Canvas(window, background="#2b2b2b", highlightthickness=0)
        self.horizontal_scroll = ttk.Scrollbar(
            window, orient="horizontal", command=self.canvas.xview
        )
        self.vertical_scroll = ttk.Scrollbar(
            window, orient="vertical", command=self.canvas.yview
        )
        self.canvas.configure(
            xscrollcommand=self.horizontal_scroll.set,
            yscrollcommand=self.vertical_scroll.set,
        )

        footer = ttk.Frame(window)
        footer.pack(side="bottom", fill="x")
        self.caption_label = ttk.Label(footer, foreground="#6b6b6b")
        self.caption_label.pack(side="left", padx=10, pady=6)
        ttk.Button(footer, text="Close", command=window.destroy).pack(
            side="right", padx=10, pady=6
        )
        ttk.Button(footer, text="1:1", width=5, command=self.zoom_actual).pack(
            side="right", padx=2, pady=6
        )
        ttk.Button(footer, text="Fit", width=5, command=self.zoom_to_fit).pack(
            side="right", padx=2, pady=6
        )

        self.vertical_scroll.pack(side="right", fill="y")
        self.horizontal_scroll.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas_image = self.canvas.create_image(0, 0, anchor="nw")

        # Windows sends <MouseWheel> with a signed delta; wheel zooms, and
        # dragging pans once the picture is bigger than the window.
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.window.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<ButtonPress-1>", self.on_drag_start)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.window.bind("<Key-plus>", lambda event: self.zoom_by(ZOOM_STEP))
        self.window.bind("<Key-minus>", lambda event: self.zoom_by(1 / ZOOM_STEP))

        self._size_window_to_image()
        self.zoom_to_fit()

    # -- sizing ------------------------------------------------------------ #
    def _size_window_to_image(self) -> None:
        """Open at the picture's own size, or the screen's, whichever is smaller."""
        screen_width = int(self.window.winfo_screenwidth() * SCREEN_MARGIN_FRACTION)
        screen_height = int(self.window.winfo_screenheight() * SCREEN_MARGIN_FRACTION)
        width = min(self.source.width + 24, screen_width)
        height = min(self.source.height + 70, screen_height)
        x = max((self.window.winfo_screenwidth() - width) // 2, 0)
        y = max((self.window.winfo_screenheight() - height) // 2, 0)
        self.window.geometry(f"{int(width)}x{int(height)}+{x}+{y}")
        self.window.update_idletasks()

    def _visible_size(self) -> tuple[int, int]:
        return (
            max(self.canvas.winfo_width(), 1),
            max(self.canvas.winfo_height(), 1),
        )

    # -- zoom -------------------------------------------------------------- #
    def zoom_to_fit(self) -> None:
        """Show the whole picture, without enlarging one smaller than the window."""
        visible_width, visible_height = self._visible_size()
        fit = min(visible_width / self.source.width, visible_height / self.source.height)
        self.set_scale(min(fit, 1.0))

    def zoom_actual(self) -> None:
        self.set_scale(1.0)

    def zoom_by(self, factor: float, anchor=None) -> None:
        self.set_scale(self.scale * factor, anchor)

    def maximum_usable_scale(self) -> float:
        """The largest zoom whose bitmap stays within the pixel ceiling."""
        source_pixels = self.source.width * self.source.height
        if source_pixels <= 0:
            return MAXIMUM_ZOOM
        return min(MAXIMUM_ZOOM, (MAXIMUM_RENDERED_PIXELS / source_pixels) ** 0.5)

    def set_scale(self, scale: float, anchor=None) -> None:
        """Change the zoom, keeping `anchor` (a widget x, y) under the pointer."""
        scale = max(MINIMUM_ZOOM, min(self.maximum_usable_scale(), scale))
        if abs(scale - self.scale) < 1e-6 and self.photo is not None:
            return

        # Work out which point of the picture is under the pointer now, so the
        # same point can be put back there afterwards. Without this the view
        # drifts away from whatever the user is trying to look at.
        image_point = None
        if anchor is not None and self.photo is not None:
            pointer_x, pointer_y = anchor
            image_point = (
                self.canvas.canvasx(pointer_x) / self.scale,
                self.canvas.canvasy(pointer_y) / self.scale,
            )

        self.scale = scale
        self._schedule_redraw(image_point, anchor)

    def _schedule_redraw(self, image_point, anchor) -> None:
        """Coalesce rapid wheel notches into a single resample."""
        if self._pending_redraw is not None:
            self.window.after_cancel(self._pending_redraw)
        self._pending_redraw = self.window.after(
            REDRAW_DELAY_MILLISECONDS, lambda: self._redraw(image_point, anchor)
        )
        self._update_caption()

    def _redraw(self, image_point, anchor) -> None:
        self._pending_redraw = None
        width = max(int(self.source.width * self.scale), 1)
        height = max(int(self.source.height * self.scale), 1)

        # Shrinking wants LANCZOS for quality; enlarging looks the same with
        # BILINEAR and costs far less on a big screenshot.
        resample = Image.LANCZOS if self.scale < 1.0 else Image.BILINEAR
        try:
            rendered = self.source.resize((width, height), resample)
            self.photo = ImageTk.PhotoImage(rendered)
        except (OSError, ValueError, MemoryError) as error:
            self.caption_label.config(text=f"Could not zoom: {error}")
            return

        self.canvas.itemconfigure(self.canvas_image, image=self.photo)
        self.canvas.configure(scrollregion=(0, 0, width, height))

        if image_point is not None and anchor is not None:
            self._restore_anchor(image_point, anchor, width, height)
        self._update_caption()

    def _restore_anchor(self, image_point, anchor, width, height) -> None:
        """Scroll so the pre-zoom point sits under the pointer again."""
        pointer_x, pointer_y = anchor
        target_x = image_point[0] * self.scale - pointer_x
        target_y = image_point[1] * self.scale - pointer_y
        if width:
            self.canvas.xview_moveto(max(0.0, min(1.0, target_x / width)))
        if height:
            self.canvas.yview_moveto(max(0.0, min(1.0, target_y / height)))

    def _update_caption(self) -> None:
        self.caption_label.config(
            text=(
                f"{os.path.basename(self.image_path)}   -   "
                f"{self.source.width}x{self.source.height}   -   "
                f"{self.scale * 100:.0f}%   -   wheel to zoom, drag to pan"
            )
        )

    # -- input ------------------------------------------------------------- #
    def on_wheel(self, event):
        factor = ZOOM_STEP if event.delta > 0 else 1 / ZOOM_STEP
        # The pointer is reported against the widget it entered, so convert to
        # canvas coordinates when the wheel arrives on the window instead.
        anchor_x = event.x
        anchor_y = event.y
        if event.widget is not self.canvas:
            anchor_x = event.x_root - self.canvas.winfo_rootx()
            anchor_y = event.y_root - self.canvas.winfo_rooty()
        self.zoom_by(factor, (anchor_x, anchor_y))
        return "break"

    def on_drag_start(self, event):
        self.canvas.scan_mark(event.x, event.y)

    def on_drag(self, event):
        self.canvas.scan_dragto(event.x, event.y, gain=1)


def image_at_click(text_widget, event) -> str:
    """The file behind the picture under the pointer, or "" if none.

    Tk cannot bind to an embedded image, so the click is resolved by asking the
    text widget what sits at that position and looking the name up in the
    registry that the renderer filled in.
    """
    registry = getattr(text_widget, "_image_files", None)
    if not registry:
        return ""
    index = text_widget.index(f"@{event.x},{event.y}")
    for start in (index, f"{index}-1c"):
        try:
            contents = text_widget.dump(start, f"{start}+1c", image=True)
        except tk.TclError:
            continue
        for kind, value, _position in contents:
            if kind == "image" and value in registry:
                return registry[value]
    return ""
