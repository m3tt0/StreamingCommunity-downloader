"""A chapter's pages as one PDF, without re-encoding what does not need it.

Pillow's own PDF writer re-encodes every page as a JPEG, which for a scan means
a second generation of compression artefacts on line art. So the file is
written here, one image per page at its own pixel size (1 px = 1 pt):

- a JPEG page (RGB or greyscale) goes in as it is, under ``DCTDecode`` — the
  bytes downloaded are the bytes in the PDF;
- anything else (PNG, WebP, a CMYK JPEG) is decoded and stored losslessly under
  ``FlateDecode``, transparency flattened on white, since a printed page has no
  alpha.

Written as a stream with the byte offsets kept for the xref table, so a
two-hundred-page chapter never has to sit in memory whole. Pillow is imported
here only: nothing else in the panel needs it.
"""

import re
import zlib
from datetime import datetime, timezone


def _text(value: str) -> bytes:
    """A PDF text string: UTF-16BE with a BOM, as hex, so any title survives."""
    return b"<FEFF" + str(value).encode("utf-16-be").hex().upper().encode() + b">"


def _image(path: str) -> tuple[dict, bytes]:
    """The image dictionary entries and the stream bytes for one page."""
    from PIL import Image

    with Image.open(path) as im:
        width, height = im.size
        if im.format == "JPEG" and im.mode in ("RGB", "L"):
            with open(path, "rb") as fh:
                data = fh.read()
            space = "/DeviceRGB" if im.mode == "RGB" else "/DeviceGray"
            return {"w": width, "h": height, "space": space, "filter": "/DCTDecode"}, data

        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            pixels, space = flat, "/DeviceRGB"
        elif im.mode in ("L", "1"):
            pixels, space = im.convert("L"), "/DeviceGray"
        else:
            pixels, space = im.convert("RGB"), "/DeviceRGB"
        data = zlib.compress(pixels.tobytes(), 6)
        return {"w": width, "h": height, "space": space, "filter": "/FlateDecode"}, data


_RAW_VALUE = re.compile(r"^[A-Za-z0-9,._-]*$")


def write_pdf(target: str, pages: list[str], info: dict | None = None,
              raw: dict | None = None) -> None:
    """Write ``pages`` (image paths, in order) to ``target`` as one PDF.

    ``info`` goes into the document information as text strings. ``raw`` holds
    private keys written as plain literal strings, which is what lets the panel
    read its own chapter manifest back with a regex instead of a PDF parser —
    so their values are restricted to characters that need no escaping.
    """
    offsets: dict[int, int] = {}
    # 1 catalog, 2 page tree, 3 info, then three objects per page.
    page_ids = [4 + 3 * i for i in range(len(pages))]

    with open(target, "wb") as out:
        def obj(number: int, body: bytes, stream: bytes | None = None):
            offsets[number] = out.tell()
            out.write(f"{number} 0 obj\n".encode() + body)
            if stream is not None:
                out.write(b"\nstream\n" + stream + b"\nendstream")
            out.write(b"\nendobj\n")

        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        kids = b" ".join(f"{p} 0 R".encode() for p in page_ids)
        obj(2, b"<< /Type /Pages /Kids [" + kids + b"] /Count "
            + str(len(pages)).encode() + b" >>")

        entries = {k: v for k, v in (info or {}).items() if v not in (None, "")}
        stamp = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ")
        body = b"<< " + b" ".join(f"/{k} ".encode() + _text(v) for k, v in entries.items())
        for key, value in (raw or {}).items():
            if not _RAW_VALUE.match(str(value)):
                raise ValueError(f"PDF raw value not allowed: {key}")
            body += f" /{key} ({value})".encode()
        obj(3, body + b" /Producer " + _text("SC Panel") + b" /CreationDate ("
            + stamp.encode() + b") >>")

        for page_id, path in zip(page_ids, pages):
            meta, data = _image(path)
            w, h = meta["w"], meta["h"]
            image_id, content_id = page_id + 1, page_id + 2
            obj(page_id, (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
                          f"/Resources << /XObject << /Im0 {image_id} 0 R >> >> "
                          f"/Contents {content_id} 0 R >>").encode())
            obj(image_id, (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                           f"/ColorSpace {meta['space']} /BitsPerComponent 8 "
                           f"/Filter {meta['filter']} /Length {len(data)} >>").encode(), data)
            content = f"q {w} 0 0 {h} 0 0 cm /Im0 Do Q".encode()
            obj(content_id, f"<< /Length {len(content)} >>".encode(), content)

        xref = out.tell()
        count = max(offsets) + 1
        out.write(f"xref\n0 {count}\n0000000000 65535 f \n".encode())
        for number in range(1, count):
            out.write(f"{offsets[number]:010d} 00000 n \n".encode())
        out.write(f"trailer\n<< /Size {count} /Root 1 0 R /Info 3 0 R >>\n"
                  f"startxref\n{xref}\n%%EOF\n".encode())
