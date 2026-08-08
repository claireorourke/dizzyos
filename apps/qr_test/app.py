"""QR test app — can a phone scan a QR code off the panels, and at what density?

Renders one static frame with two codes side by side so a single flash of the
hardware answers both questions:

  left   Version 9 at 1 LED px per module — the stress test. 53x53 modules is
         the densest code that fits the 64px height with its quiet zone (61x61);
         182 bytes at EC M, enough for full URLs with paths.
  right  Version 2 at 1 LED px per module (33x33) — the baseline 1px variant. If
         bloom and the LED bezel grid don't defeat it, full lowercase URLs fit.

Dark modules are unlit (the decoder wants dark-on-light), light modules render at
`white_level` so bloom can be tuned without touching the global panel brightness.
"""

from PIL import ImageDraw

from kernel.app import App

QUIET = 4  # quiet-zone width in modules, per the QR spec

# (config key, QR version, error-correction level, LED px per module)
# Both get level M so a few bloom-corrupted modules don't kill the read.
VARIANTS = [
    ("url_stress", 9, "M", 1),
    ("url_1px", 2, "M", 1),
]


def _matrix(payload, version, ec_level):
    """Return the QR module grid (True = dark) including the quiet zone."""
    import qrcode

    qr = qrcode.QRCode(
        version=version,
        error_correction=getattr(qrcode.constants, f"ERROR_CORRECT_{ec_level}"),
        border=QUIET,
    )
    qr.add_data(payload)
    qr.make(fit=False)  # raise instead of silently growing past the panel
    return qr.get_matrix()


class QrTestApp(App):
    def on_start(self, services):
        super().on_start(services)
        self._frame = None
        self._error = None
        try:
            self._codes = [
                (_matrix(self.config.get(key, ""), version, ec), scale)
                for key, version, ec, scale in VARIANTS
            ]
        except ImportError:
            self._error = "pip install qrcode"
        except Exception as exc:  # DataOverflowError: payload too long for version
            self._error = str(exc)

    def _draw_code(self, image, matrix, scale, x, y, white):
        px = image.load()
        for row, cells in enumerate(matrix):
            for col, dark in enumerate(cells):
                if dark:
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        px[x + col * scale + dx, y + row * scale + dy] = white

    def _build(self):
        frame = self.blank()
        if self._error:
            draw = ImageDraw.Draw(frame)
            pf = self.services.fonts.pixel()
            pf.draw_text(draw, 2, 2, "QR TEST", (255, 120, 120))
            pf.draw_text(draw, 2, 12, self._error[:40], (200, 200, 200))
            return frame

        level = int(self.config.get("white_level", 255))
        white = (level, level, level)
        pf = self.services.fonts.pixel()
        draw = ImageDraw.Draw(frame)

        # Left: the dense stress-test code, vertically centered against the edge.
        big, big_scale = self._codes[0]
        big_size = len(big) * big_scale
        self._draw_code(frame, big, big_scale, 2, (frame.height - big_size) // 2, white)

        # Right: the 1px code centered in the leftover column, label beneath.
        small, small_scale = self._codes[1]
        small_size = len(small) * small_scale
        col_x = 2 + big_size
        cx = col_x + (frame.width - col_x - small_size) // 2
        self._draw_code(frame, small, small_scale, cx, 8, white)
        label = "1PX V2"
        lx = col_x + (frame.width - col_x - pf.measure(label)) // 2
        pf.draw_text(draw, lx, 8 + small_size + 5, label, (170, 170, 170))
        return frame

    def render(self, t):
        if self._frame is None:
            self._frame = self._build()
        return self._frame
