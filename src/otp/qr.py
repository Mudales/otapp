"""QR decoding from encoded image bytes (photos, screenshots, camera frames)."""

import io

from PIL import Image, ImageOps
from pyzbar.pyzbar import ZBarSymbol, decode


def decode_qr(image_bytes: bytes) -> list[str]:
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img).convert("L")
    # Big photos slow zbar down a lot without improving detection
    img.thumbnail((1600, 1600))
    results = decode(img, symbols=[ZBarSymbol.QRCODE])
    if not results:
        # Dark-mode / inverted QR codes
        results = decode(ImageOps.invert(img), symbols=[ZBarSymbol.QRCODE])
    return [r.data.decode("utf-8", errors="replace") for r in results]
