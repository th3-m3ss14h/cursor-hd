#!/usr/bin/env python3
"""
Xcursor binary format extractor.

Parses raw Xcursor files (the compiled binaries found under a theme's
cursors/ dir) and dumps every frame as a PNG plus a sidecar JSON with
per-frame nominal size, actual width/height, hotspot, and delay (ms).

Format reference (all fields little-endian regardless of host):

File header:
    magic       4 bytes  "Xcur"
    header_size u32      == 16
    version     u32
    ntoc        u32      number of table-of-contents entries

TOC entry (12 bytes each, ntoc of them):
    type        u32      0xfffd0002 = image chunk, 0xfffe0001 = comment
    subtype     u32      for images: nominal size (e.g. 32)
    position    u32      absolute file offset of the chunk

Image chunk (at `position`):
    header_size u32      == 36
    type        u32      0xfffd0002
    subtype     u32      nominal size (redundant w/ TOC)
    version     u32      == 1
    width       u32      actual pixel width  (<= 0x7fff)
    height      u32      actual pixel height (<= 0x7fff)
    xhot        u32      hotspot x
    yhot        u32      hotspot y
    delay       u32      animation delay in ms
    pixels      width*height*4 bytes, packed 32-bit ARGB, premultiplied
                alpha, stored byte-order B,G,R,A per pixel.
"""
import json
import os
import struct
import sys

MAGIC = b"Xcur"
IMAGE_TYPE = 0xfffd0002
COMMENT_TYPE = 0xfffe0001


def parse_xcursor(path):
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != MAGIC:
        raise ValueError(f"{path}: not an Xcursor file (bad magic)")

    if len(data) < 16:
        raise ValueError(f"{path}: truncated Xcursor header")

    header_size, version, ntoc = struct.unpack_from("<III", data, 4)
    if header_size != 16:
        raise ValueError(f"{path}: unexpected header size {header_size}")
    if ntoc > (len(data) - 16) // 12:
        raise ValueError(f"{path}: truncated TOC (declares {ntoc} entries)")

    toc = []
    off = 16
    for _ in range(ntoc):
        ctype, subtype, position = struct.unpack_from("<III", data, off)
        toc.append((ctype, subtype, position))
        off += 12

    frames = []
    for ctype, subtype, position in toc:
        if ctype != IMAGE_TYPE:
            continue  # skip comment chunks etc.

        if position > len(data) - 36:
            raise ValueError(f"{path}: image chunk header at {position} is outside the file")

        (chunk_header_size, chunk_type, nominal_size, chunk_version,
         width, height, xhot, yhot, delay) = struct.unpack_from(
            "<IIIIIIIII", data, position
        )
        if chunk_header_size != 36:
            raise ValueError(f"{path}: unexpected image chunk header size {chunk_header_size} at {position}")
        if chunk_type != IMAGE_TYPE:
            raise ValueError(f"{path}: TOC/chunk type mismatch at {position}")
        if chunk_version != 1:
            raise ValueError(f"{path}: unsupported image chunk version {chunk_version} at {position}")
        if nominal_size == 0:
            raise ValueError(f"{path}: image chunk at {position} has zero nominal size")
        if width == 0 or height == 0 or width > 0x7fff or height > 0x7fff:
            raise ValueError(f"{path}: invalid image dimensions {width}x{height} at {position}")
        if xhot >= width or yhot >= height:
            raise ValueError(f"{path}: invalid hotspot ({xhot},{yhot}) for {width}x{height} at {position}")

        pixel_off = position + chunk_header_size
        n_pixels = width * height
        if n_pixels > (len(data) - pixel_off) // 4:
            raise ValueError(
                f"{path}: truncated pixel data at offset {position} "
                f"(wanted {n_pixels * 4} bytes)"
            )
        pixel_bytes = data[pixel_off:pixel_off + n_pixels * 4]
        if len(pixel_bytes) != n_pixels * 4:
            raise ValueError(
                f"{path}: truncated pixel data at offset {position} "
                f"(wanted {n_pixels*4}, got {len(pixel_bytes)})"
            )

        frames.append({
            "nominal_size": nominal_size,
            "width": width,
            "height": height,
            "xhot": xhot,
            "yhot": yhot,
            "delay": delay,
            "pixels_bgra_premult": pixel_bytes,
        })

    # Sort by (nominal_size, position-order) so animation frames for a given
    # nominal size stay in file order (that order == animation order).
    return frames


def scan_xcursor_toc(path):
    """
    Cheap header-only scan: magic/TOC + per-image chunk headers, no pixel
    decode. Returns a list of dicts: nominal_size, width, height, delay.
    Used by the theme scanner to grade native resolution across an entire
    installed theme without decoding megabytes of pixel data per cursor.
    """
    with open(path, "rb") as f:
        data = f.read(16)
        if data[:4] != MAGIC:
            raise ValueError(f"{path}: not an Xcursor file (bad magic)")
        if len(data) < 16:
            raise ValueError(f"{path}: truncated Xcursor header")
        header_size, version, ntoc = struct.unpack_from("<III", data, 4)
        if header_size != 16:
            raise ValueError(f"{path}: unexpected header size {header_size}")

        # Check the declared TOC count against the actual file size before
        # using it to size a read. A corrupt ntoc can otherwise cause a very
        # large allocation even though the file itself is tiny.
        file_size = os.fstat(f.fileno()).st_size
        if ntoc > (file_size - 16) // 12:
            raise ValueError(f"{path}: truncated TOC (declares {ntoc} entries)")

        toc = f.read(ntoc * 12)
        if len(toc) != ntoc * 12:
            raise ValueError(f"{path}: truncated TOC (declares {ntoc} entries)")
        entries = []
        for i in range(ntoc):
            ctype, subtype, position = struct.unpack_from("<III", toc, i * 12)
            if ctype != IMAGE_TYPE:
                continue
            entries.append(position)

        frames = []
        for position in entries:
            if position > file_size - 36:
                raise ValueError(f"{path}: image chunk header at {position} is outside the file")
            f.seek(position)
            chunk = f.read(36)
            if len(chunk) != 36:
                raise ValueError(f"{path}: truncated image chunk header at {position}")
            (chunk_header_size, chunk_type, nominal_size, chunk_version,
             width, height, xhot, yhot, delay) = struct.unpack("<IIIIIIIII", chunk)
            if chunk_header_size != 36 or chunk_type != IMAGE_TYPE or chunk_version != 1:
                raise ValueError(f"{path}: invalid image chunk header at {position}")
            if nominal_size == 0 or width == 0 or height == 0 or width > 0x7fff or height > 0x7fff:
                raise ValueError(f"{path}: invalid image metadata at {position}: {width}x{height}, nominal {nominal_size}")
            if xhot >= width or yhot >= height:
                raise ValueError(f"{path}: invalid hotspot ({xhot},{yhot}) at {position}")
            if width * height > (file_size - position - chunk_header_size) // 4:
                raise ValueError(f"{path}: truncated pixel data at offset {position}")
            frames.append({
                "nominal_size": nominal_size,
                "width": width,
                "height": height,
                "delay": delay,
            })
        return frames


def bgra_premult_to_rgba(pixel_bytes, width, height):
    """Convert packed premultiplied-alpha BGRA -> straight-alpha RGBA bytes."""
    out = bytearray(len(pixel_bytes))
    for i in range(0, len(pixel_bytes), 4):
        b, g, r, a = pixel_bytes[i:i + 4]
        if a == 0:
            out[i:i + 4] = (0, 0, 0, 0)
        else:
            # un-premultiply
            r = min(255, round(r * 255 / a))
            g = min(255, round(g * 255 / a))
            b = min(255, round(b * 255 / a))
            out[i:i + 4] = (r, g, b, a)
    return bytes(out)


def dump_cursor(path, out_dir, cursor_name):
    from PIL import Image

    frames = parse_xcursor(path)
    os.makedirs(out_dir, exist_ok=True)

    # group frames by nominal size (a theme can ship multiple sizes of the
    # same cursor, e.g. 24px and 32px variants, each with its own animation)
    by_size = {}
    for fr in frames:
        by_size.setdefault(fr["nominal_size"], []).append(fr)

    manifest = {"source": path, "sizes": {}}

    for nominal_size, fr_list in by_size.items():
        size_manifest = []
        for frame_idx, fr in enumerate(fr_list):
            rgba = bgra_premult_to_rgba(
                fr["pixels_bgra_premult"], fr["width"], fr["height"]
            )
            img = Image.frombytes("RGBA", (fr["width"], fr["height"]), rgba)
            fname = f"{cursor_name}.size{nominal_size}.frame{frame_idx:03d}.png"
            img.save(os.path.join(out_dir, fname))
            size_manifest.append({
                "file": fname,
                "width": fr["width"],
                "height": fr["height"],
                "xhot": fr["xhot"],
                "yhot": fr["yhot"],
                "delay": fr["delay"],
            })
        manifest["sizes"][str(nominal_size)] = size_manifest

    with open(os.path.join(out_dir, f"{cursor_name}.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    return manifest


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <xcursor_file> <out_dir> <cursor_name>",
              file=sys.stderr)
        sys.exit(1)
    m = dump_cursor(sys.argv[1], sys.argv[2], sys.argv[3])
    print(json.dumps(m, indent=2))
