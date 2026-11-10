"""Strip metadata from an uploaded picture before it is stored (owner, 2026-11-10).

EXIF and XMP can hold where a photo was taken (GPS), the device and its serial, comments. They are removed from the
bytes without decoding the picture, so the pixels stay exactly as they were:

- JPEG: APP1 (EXIF, XMP), APP13 (IPTC) and comment segments go; a minimal EXIF with only the orientation comes back
  when the photo is stored on its side, so it still shows upright. Colour profiles (APP2) stay.
- PNG: eXIf and the text chunks (tEXt, zTXt, iTXt, where XMP lives) go.
- WebP: the EXIF and XMP chunks go, and the extended header no longer announces them.

Anything that does not parse as expected is returned unchanged only when it holds no metadata marker at all;
otherwise the caller gets ValueError (the upload is refused rather than stored with its metadata)."""

from __future__ import annotations

import struct

_JPEG_DROP = {0xE1, 0xED, 0xFE}  # APP1 (EXIF, XMP), APP13 (IPTC), COM
_PNG_DROP = {b"eXIf", b"tEXt", b"zTXt", b"iTXt"}
_WEBP_DROP = {b"EXIF", b"XMP "}


def strip_metadata(data: bytes, content_type: str) -> bytes:
    if content_type == "image/jpeg":
        return _jpeg(data)
    if content_type == "image/png":
        return _png(data)
    if content_type == "image/webp":
        return _webp(data)
    return data  # GIF carries no location; everything else is not a picture we keep as one


def _orientation_app1(o: int) -> bytes:
    # "Exif\0\0" + big-endian TIFF header + one IFD entry: Orientation (0x0112), SHORT, count 1, value o
    tiff = b"MM\x00*" + struct.pack(">I", 8) + struct.pack(">H", 1) \
        + struct.pack(">HHIHH", 0x0112, 3, 1, o, 0) + struct.pack(">I", 0)
    body = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(body) + 2) + body


def _exif_orientation(seg: bytes) -> int:
    """The orientation in an APP1 EXIF segment body (after the length), 1 when absent or unreadable."""
    if not seg.startswith(b"Exif\x00\x00") or len(seg) < 14:
        return 1
    t = seg[6:]
    end = {b"MM": ">", b"II": "<"}.get(t[:2])
    if end is None:
        return 1
    try:
        off = struct.unpack(end + "I", t[4:8])[0]
        n = struct.unpack(end + "H", t[off:off + 2])[0]
        for i in range(min(n, 200)):
            e = t[off + 2 + 12 * i: off + 14 + 12 * i]
            tag, typ, _count = struct.unpack(end + "HHI", e[:8])
            if tag == 0x0112 and typ == 3:
                v = struct.unpack(end + "H", e[8:10])[0]
                return v if 1 <= v <= 8 else 1
    except (struct.error, IndexError):
        return 1
    return 1


def _jpeg(data: bytes) -> bytes:
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    out = [b"\xff\xd8"]
    orientation = 1
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            raise ValueError("JPEG segment marker expected")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xDA or marker == 0xD9:  # start of scan (or end): the rest is picture data
            rest = data[i:]
            if orientation != 1:
                out.insert(1, _orientation_app1(orientation))
            return b"".join(out) + rest
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # markers without a length
            out.append(data[i:i + 2])
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        seg = data[i:i + 2 + length]
        if len(seg) != 2 + length:
            raise ValueError("JPEG segment runs past the end")
        if marker == 0xE1 and orientation == 1:
            orientation = _exif_orientation(seg[4:])
        if marker not in _JPEG_DROP:
            out.append(seg)
        i += 2 + length
    raise ValueError("JPEG without picture data")


def _png(data: bytes) -> bytes:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    out = [data[:8]]
    i = 8
    while i + 8 <= len(data):
        (length,) = struct.unpack(">I", data[i:i + 4])
        ctype = data[i + 4:i + 8]
        chunk = data[i:i + 12 + length]
        if len(chunk) != 12 + length:
            raise ValueError("PNG chunk runs past the end")
        if ctype not in _PNG_DROP:
            out.append(chunk)
        i += 12 + length
        if ctype == b"IEND":
            return b"".join(out)
    raise ValueError("PNG without IEND")


def _webp(data: bytes) -> bytes:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP")
    chunks = []
    i = 12
    while i + 8 <= len(data):
        fourcc = data[i:i + 4]
        (size,) = struct.unpack("<I", data[i + 4:i + 8])
        whole = data[i:i + 8 + size + (size & 1)]
        if len(whole) < 8 + size:
            raise ValueError("WebP chunk runs past the end")
        if fourcc not in _WEBP_DROP:
            if fourcc == b"VP8X" and size >= 1:
                flags = whole[8] & ~(0x08 | 0x04)  # no EXIF, no XMP
                whole = whole[:8] + bytes([flags]) + whole[9:]
            chunks.append(whole)
        i += 8 + size + (size & 1)
    body = b"WEBP" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body
