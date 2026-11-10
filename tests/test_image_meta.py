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


def test_jpeg_drops_everything_after_the_picture_and_between_scans():
    raw = _jpeg(1)
    # an iPhone-style second image after the first one's end, with its own EXIF and GPS
    tail = _jpeg(6).replace(b"PhoneMaker", b"SecondCam!")
    out = strip_metadata(raw + tail, "image/jpeg")
    assert out.endswith(b"\xff\xd9") and b"SecondCam!" not in out and out.count(b"\xff\xd8") == 1
    # MPF (APP2 "MPF\0", the index of those extra images) goes too
    mpf = b"\xff\xe2" + (4 + 2 + 8).to_bytes(2, "big") + b"MPF\x00" + b"II*\x00\x08\x00\x00\x00"
    out = strip_metadata(raw[:2] + mpf + raw[2:], "image/jpeg")
    assert b"MPF\x00" not in out
    # a progressive JPEG has several scans; metadata between them goes as well
    im = Image.new("RGB", (64, 48), (90, 30, 160))
    buf = io.BytesIO()
    im.save(buf, "JPEG", progressive=True, quality=85)
    prog = buf.getvalue()
    sos2 = prog.index(b"\xff\xda", prog.index(b"\xff\xda") + 2)
    sneaky = b"\xff\xfe" + (2 + 9).to_bytes(2, "big") + b"GPS:52,5;"
    out = strip_metadata(prog[:sos2] + sneaky + prog[sos2:], "image/jpeg")
    assert b"GPS:52,5;" not in out
    assert Image.open(io.BytesIO(out)).size == (64, 48)
    Image.open(io.BytesIO(out)).load()  # still decodes


def test_gif_loses_comments_and_xmp_but_keeps_looping():
    frames = [Image.new("RGB", (6, 6), c) for c in ((255, 0, 0), (0, 0, 255))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], loop=0, comment=b"shot at home")
    gif = buf.getvalue()
    xmp = b"\x21\xff\x0bXMP DataXMP" + b"\x05where" + b"\x00"
    k = gif.index(b"\x21\xff\x0bNETSCAPE")  # just before the looping block
    out = strip_metadata(gif[:k] + xmp + gif[k:], "image/gif")
    assert b"shot at home" not in out and b"XMP DataXMP" not in out and b"where" not in out
    assert b"NETSCAPE2.0" in out, "looping kept"
    g = Image.open(io.BytesIO(out))
    assert g.n_frames == 2


def test_malformed_pictures_are_refused_cleanly_and_fast():
    import time
    im = Image.new("RGB", (16, 12), (1, 2, 3))
    samples = {"image/jpeg": _jpeg(6)}
    for fmt, ct in (("PNG", "image/png"), ("WEBP", "image/webp"), ("GIF", "image/gif")):
        buf = io.BytesIO()
        im.save(buf, fmt)
        samples[ct] = buf.getvalue()
    t = time.monotonic()
    for ct, data in samples.items():
        for cut in range(1, len(data)):
            try:
                strip_metadata(data[:cut], ct)
            except ValueError:
                pass
        for k in range(0, len(data), 7):  # a flipped byte anywhere
            bad = data[:k] + bytes([data[k] ^ 0xFF]) + data[k + 1:]
            try:
                strip_metadata(bad, ct)
            except ValueError:
                pass
    assert time.monotonic() - t < 10


def test_jpeg_keeps_only_what_decodes_and_colours_the_picture():
    raw = _jpeg(1)
    app5 = b"\xff\xe5" + (2 + 10).to_bytes(2, "big") + b"VENDOR-GPS"
    icc = b"\xff\xe2" + (2 + 14).to_bytes(2, "big") + b"ICC_PROFILE\x00\x01\x01"
    out = strip_metadata(raw[:2] + app5 + icc + raw[2:], "image/jpeg")
    assert b"VENDOR-GPS" not in out and b"ICC_PROFILE\x00" in out
    Image.open(io.BytesIO(out)).load()
