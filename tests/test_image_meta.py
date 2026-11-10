"""Photos lose their metadata on upload (owner, 2026-11-10): EXIF can hold the GPS position where a photo was taken.
Stripped from the bytes, without decoding: the pixels stay exactly as they were; a JPEG keeps only its orientation, so
a phone photo still shows upright."""

from __future__ import annotations

import io

from PIL import Image

from notify.chat_files import NewFile, ChatFiles
from notify.imagemeta import strip_metadata

GPS = {1: "N", 2: (52.0, 5.0, 0.0), 3: "E", 4: (5.0, 10.0, 0.0)}


def _jpeg(orientation: int = 6) -> bytes:
    im = Image.new("RGB", (60, 40), (200, 40, 40))
    ex = Image.Exif()
    ex[0x0112] = orientation
    ex[0x010F] = "PhoneMaker"
    ex.get_ifd(0x8825).update(GPS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90, exif=ex, comment=b"note")
    return buf.getvalue()


def _scan(jpeg: bytes) -> bytes:
    return jpeg[jpeg.index(b"\xff\xda"):]  # from the start of scan: the compressed pixels


def test_jpeg_loses_gps_and_maker_but_keeps_orientation_and_pixels():
    raw = _jpeg(6)
    assert Image.open(io.BytesIO(raw)).getexif().get_ifd(0x8825), "the test photo carries GPS"
    out = strip_metadata(raw, "image/jpeg")
    ex = Image.open(io.BytesIO(out)).getexif()
    assert not ex.get_ifd(0x8825) and 0x010F not in ex
    assert ex.get(0x0112) == 6
    assert _scan(out) == _scan(raw), "pixels untouched (no re-encoding)"
    assert b"PhoneMaker" not in out


def test_upright_jpeg_keeps_no_exif_at_all():
    out = strip_metadata(_jpeg(1), "image/jpeg")
    assert not Image.open(io.BytesIO(out)).getexif()


def test_png_and_webp_lose_their_metadata_chunks():
    im = Image.new("RGB", (8, 8), (10, 120, 200))
    ex = Image.Exif()
    ex.get_ifd(0x8825).update(GPS)
    from PIL.PngImagePlugin import PngInfo
    info = PngInfo()
    info.add_text("Comment", "taken at home")
    buf = io.BytesIO()
    im.save(buf, "PNG", exif=ex, pnginfo=info)
    png = strip_metadata(buf.getvalue(), "image/png")
    assert b"eXIf" not in png and b"taken at home" not in png
    assert Image.open(io.BytesIO(png)).size == (8, 8)
    buf = io.BytesIO()
    im.save(buf, "WEBP", exif=ex, lossless=True)
    webp = strip_metadata(buf.getvalue(), "image/webp")
    assert b"EXIF" not in webp[12:]
    w = Image.open(io.BytesIO(webp))
    assert w.size == (8, 8) and not w.getexif().get_ifd(0x8825)
    assert int.from_bytes(webp[4:8], "little") == len(webp) - 8, "RIFF size updated"


def test_uploads_are_stored_without_gps():
    p = ChatFiles._prepare(NewFile("IMG_0001.jpg", "image/jpeg", _jpeg(6)), 0)
    assert p.kind == "image" and (p.width, p.height) == (40, 60), "shown size, turned"
    assert not Image.open(io.BytesIO(p.data)).getexif().get_ifd(0x8825)


def test_non_images_are_left_alone():
    data = b"Exif\x00\x00 not a picture"
    assert strip_metadata(data, "application/octet-stream") == data
