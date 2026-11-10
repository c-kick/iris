"""Strip metadata from an uploaded picture before it is stored (owner, 2026-11-10).

EXIF and XMP can hold where a photo was taken (GPS), the device and its serial, comments. They are removed from the
bytes without decoding the picture, so the pixels stay exactly as they were:

- JPEG: only what decodes and colours the picture is kept (frame headers, tables, scans, JFIF, an ICC profile,
  Adobe's colour transform); every other segment goes (EXIF, XMP, IPTC, MPF, comments, unknown ones), wherever it is,
  and nothing after the first end-of-image survives; a minimal EXIF with only the orientation comes back when the photo is stored on its side, so
  it still shows upright. Colour profiles (APP2 ICC) stay.
- GIF: comments and application blocks go (XMP, ICC), except the one that makes an animation loop.
- PNG: eXIf and the text chunks (tEXt, zTXt, iTXt, where XMP lives) go.
- WebP: the EXIF and XMP chunks go, and the extended header no longer announces them.

Anything that does not parse as expected is returned unchanged only when it holds no metadata marker at all;
otherwise the caller gets ValueError (the upload is refused rather than stored with its metadata)."""

from __future__ import annotations

import struct

# what a JPEG needs to decode and colour-manage: frame headers (SOF0-15 without C4/C8/CC), Huffman and arithmetic
# tables, quantisation tables, restart interval, expand reference; of the APPn segments only JFIF, an ICC profile and
# Adobe's colour transform. Everything else (EXIF, XMP, IPTC, MPF, maker notes, JUMBF, comments, unknown) goes.
_JPEG_TABLES = {0xC4, 0xCC, 0xDB, 0xDD, 0xDF}
_JPEG_APPS = {0xE0: (b"JFIF\x00", b"JFXX\x00"), 0xE2: (b"ICC_PROFILE\x00",), 0xEE: (b"Adobe",)}


def _jpeg_keeps(marker: int, body: bytes) -> bool:
    if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
        return True  # a frame header
    if marker in _JPEG_TABLES:
        return True
    return any(body.startswith(sig) for sig in _JPEG_APPS.get(marker, ()))
_PNG_DROP = {b"eXIf", b"tEXt", b"zTXt", b"iTXt"}
_WEBP_DROP = {b"EXIF", b"XMP "}


def strip_metadata(data: bytes, content_type: str) -> bytes:
    try:
        return _strip(data, content_type)
    except ValueError:
        raise
    except (IndexError, struct.error) as e:  # a malformed file is refused, never a server error
        raise ValueError(f"malformed picture: {e}") from None


def _strip(data: bytes, content_type: str) -> bytes:
    if content_type == "image/jpeg":
        return _jpeg(data)
    if content_type == "image/png":
        return _png(data)
    if content_type == "image/webp":
        return _webp(data)
    if content_type == "image/gif":
        return _gif(data)
    return data  # not a picture we keep as one


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
    """Walks the whole file: every scan's picture data is kept, metadata segments go wherever they are (also between
    the scans of a progressive JPEG), and nothing after the first end-of-image marker survives (iPhone photos append
    whole second images there, each with its own EXIF and GPS)."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    out = [b"\xff\xd8"]
    orientation = 1
    scanned = False
    n = len(data)
    i = 2
    while i < n:
        if data[i] != 0xFF:
            raise ValueError("JPEG segment marker expected")
        while i + 1 < n and data[i + 1] == 0xFF:  # fill bytes before a marker
            i += 1
        if i + 1 >= n:
            break
        marker = data[i + 1]
        if marker == 0xD9:  # end of image: whatever follows is not this picture
            out.append(b"\xff\xd9")
            break
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:  # markers without a length
            out.append(data[i:i + 2])
            i += 2
            continue
        if i + 4 > n:
            raise ValueError("JPEG segment runs past the end")
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        if length < 2 or i + 2 + length > n:
            raise ValueError("JPEG segment runs past the end")
        seg = data[i:i + 2 + length]
        i += 2 + length
        if marker == 0xDA:  # start of scan: the header, then entropy-coded data up to the next real marker
            out.append(seg)
            j = i
            while True:
                j = data.find(b"\xff", j)
                if j < 0 or j + 1 >= n:
                    j = n
                    break
                nxt = data[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:  # a stuffed 0xFF or a restart marker: still picture data
                    j += 2
                    continue
                break
            out.append(data[i:j])
            i = j
            scanned = True
            continue
        if marker == 0xE1 and orientation == 1:
            orientation = _exif_orientation(seg[4:])
        if _jpeg_keeps(marker, seg[4:]):
            out.append(seg)
    if not scanned:
        raise ValueError("JPEG without picture data")
    if out[-1] != b"\xff\xd9":
        out.append(b"\xff\xd9")
    if orientation != 1:
        out.insert(1, _orientation_app1(orientation))
    return b"".join(out)


def _gif_blocks(data: bytes, i: int) -> int:
    """The index after a run of GIF data sub-blocks starting at i (size byte, bytes, ..., 0)."""
    n = len(data)
    while True:
        if i >= n:
            raise ValueError("GIF sub-blocks run past the end")
        size = data[i]
        i += 1
        if size == 0:
            return i
        i += size


_GIF_KEEP_APPS = (b"NETSCAPE2.0", b"ANIMEXTS1.0")  # looping; every other application block (XMP, ICC...) goes


def _gif(data: bytes) -> bytes:
    if data[:6] not in (b"GIF87a", b"GIF89a") or len(data) < 13:
        raise ValueError("not a GIF")
    packed = data[10]
    i = 13 + (3 * 2 ** ((packed & 7) + 1) if packed & 0x80 else 0)
    out = [data[:i]]
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x3B:  # trailer: nothing after it survives
            out.append(b"\x3b")
            return b"".join(out)
        if b == 0x21:
            if i + 2 > n:
                raise ValueError("GIF extension runs past the end")
            label = data[i + 1]
            end = _gif_blocks(data, i + 2)
            keep = label in (0xF9, 0x01) or (label == 0xFF and data[i + 3:i + 14] in _GIF_KEEP_APPS)
            if keep:
                out.append(data[i:end])
            i = end
            continue
        if b == 0x2C:  # image descriptor (+ local colour table), LZW code size, picture sub-blocks
            if i + 10 > n:
                raise ValueError("GIF image runs past the end")
            lp = data[i + 9]
            k = i + 10 + (3 * 2 ** ((lp & 7) + 1) if lp & 0x80 else 0)
            end = _gif_blocks(data, k + 1)
            out.append(data[i:end])
            i = end
            continue
        raise ValueError("unknown GIF block")
    out.append(b"\x3b")
    return b"".join(out)


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
