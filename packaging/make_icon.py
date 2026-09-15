# SPDX-FileCopyrightText: 2026 AngrySandhill
# SPDX-License-Identifier: GPL-3.0-or-later
"""Writes packaging/tapewright.ico, the title bar's cassette, for the installer and the shortcuts it makes.

    python packaging/make_icon.py

Each size is theme.cassette_pixels at that size. Sizes up to 64 are 32-bit BGRA bitmaps with the 1-bit AND
mask older readers still use, and 256 is a PNG, which is how an .ico holds its biggest size. Inno Setup's
help recommends at least 16, 32, 48, 64 and 256 for SetupIconFile; 24 is the small icon at 150% scaling.

The same pixels always make the same bytes, and tests/test_packaging.py compares a fresh drawing with the
committed file on every Python it runs on. That rules out zlib.compress for the PNG: Python 3.14 on Windows
is built with zlib-ng, which compresses the same data at the same level into different bytes (measured: level
6 differed between 3.14.5 on Windows and 3.14.4 on Linux). So the deflate stream is written here, with
deflate's fixed Huffman codes and repeats at a few set distances, and zlib only adds up the checksums.
"""

import struct
import sys
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 256)
OUT = Path(__file__).resolve().with_name("tapewright.ico")
PNG_FROM = 256  # an .ico entry this big is a PNG
LONGEST = 258  # the longest repeat deflate can say


def _table(symbols, first, extra_bits):
    """(symbol, extra bits, smallest value) for deflate's length or distance symbols, RFC 1951 3.2.5."""
    rows = []
    for symbol in symbols:
        extra = extra_bits(symbol)
        rows.append((symbol, extra, first))
        first += 1 << extra
    return rows


LENGTHS = _table(range(257, 285), 3, lambda symbol: 0 if symbol < 265 else (symbol - 261) // 4)
LENGTHS.append((285, 0, 258))  # 258 has a symbol of its own
DISTANCES = _table(range(30), 1, lambda symbol: 0 if symbol < 4 else (symbol - 2) // 2)


def _row(table, value):
    """The row a length or distance falls in: the last whose smallest value it reaches."""
    return [row for row in table if row[2] <= value][-1]


class _Bits:
    """Deflate's bit order: numbers go in from their lowest bit, Huffman codes from their highest."""

    def __init__(self):
        self.out, self.value, self.count = bytearray(), 0, 0

    def number(self, value, width):
        self.value |= value << self.count
        self.count += width
        while self.count >= 8:
            self.out.append(self.value & 0xFF)
            self.value >>= 8
            self.count -= 8

    def code(self, code, width):
        self.number(int(format(code, f"0{width}b")[::-1], 2), width)

    def symbol(self, symbol):
        """A literal or length symbol, in deflate's fixed Huffman code."""
        if symbol < 144:
            self.code(0x30 + symbol, 8)
        elif symbol < 256:
            self.code(0x190 + symbol - 144, 9)
        elif symbol < 280:
            self.code(symbol - 256, 7)
        else:
            self.code(0xC0 + symbol - 280, 8)

    def finish(self):
        if self.count:
            self.out.append(self.value)
        return bytes(self.out)


def deflate(data, distances):
    """data as a single deflate block with fixed Huffman codes.

    At each byte it takes the longest repeat of the bytes one of the distances back, when that is 3 or more
    long, and otherwise the byte itself. Nothing else is searched for, so the same data and distances give
    the same bytes whichever zlib Python has.
    """
    bits = _Bits()
    bits.number(1, 1)  # the last block
    bits.number(1, 2)  # compressed with the fixed codes
    at = 0
    while at < len(data):
        best, back = 0, 0
        for distance in distances:
            length = 0
            if distance <= at:
                while (length < LONGEST and at + length < len(data)
                       and data[at + length] == data[at + length - distance]):
                    length += 1
            if length > best:
                best, back = length, distance
        if best < 3:
            bits.symbol(data[at])
            at += 1
            continue
        symbol, extra, first = _row(LENGTHS, best)
        bits.symbol(symbol)
        bits.number(best - first, extra)
        symbol, extra, first = _row(DISTANCES, back)
        bits.code(symbol, 5)
        bits.number(back - first, extra)
        at += best
    bits.symbol(256)  # the end of the block
    return bits.finish()


def zlib_stream(data, distances):
    """data as a zlib stream (RFC 1950), which is what a PNG's IDAT chunk holds."""
    return b"\x78\x01" + deflate(data, distances) + struct.pack(">I", zlib.adler32(data))


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(pixels):
    """A square of '#rrggbb' or None (clear) as an 8-bit RGBA PNG."""
    size = len(pixels)
    rgba = b"".join(b"\0" + b"".join(bytes.fromhex(color[1:]) + b"\xff" if color else bytes(4)
                                     for color in row)
                    for row in pixels)  # each row starts with filter type 0, none
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    # A repeat is most often the pixel before or the one above.
    idat = zlib_stream(rgba, (4, 1 + 4 * size))
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def bitmap(pixels):
    """A square of '#rrggbb' or None (clear) as an .ico's bitmap entry: BITMAPINFOHEADER, BGRA, AND mask.

    Rows go bottom-up, and the header counts twice the height, the colors and then the mask. In the mask a
    set bit is a clear pixel, and each row is padded to a whole number of 32-bit words.
    """
    size = len(pixels)
    colors = b"".join(bytes.fromhex(color[5:7] + color[3:5] + color[1:3]) + b"\xff" if color else bytes(4)
                      for row in reversed(pixels) for color in row)
    mask = bytearray()
    for row in reversed(pixels):
        line = bytearray((size + 31) // 32 * 4)
        for x, color in enumerate(row):
            if color is None:
                line[x // 8] |= 0x80 >> x % 8
        mask += line
    header = struct.pack("<IiiHHIIiiII", 40, size, 2 * size, 1, 32, 0, len(colors) + len(mask), 0, 0, 0, 0)
    return header + colors + bytes(mask)


def icon_bytes(draw, sizes=SIZES):
    """The .ico file: its directory, then each size drawn by draw(size) as rows of '#rrggbb' or None."""
    images = [png(draw(size)) if size >= PNG_FROM else bitmap(draw(size)) for size in sizes]
    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = len(directory) + 16 * len(images)
    for size, image in zip(sizes, images):
        # A width or height of 256 is written as 0.
        directory += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    return directory + b"".join(images)


def main():
    sys.path.insert(0, str(OUT.parents[1]))
    from tapewright import theme  # here: it imports tkinter, and the tests load this file even without Tk

    data = icon_bytes(theme.cassette_pixels)
    OUT.write_bytes(data)
    print(f"Wrote {OUT}: {len(data):,} bytes, at {', '.join(map(str, SIZES))} pixels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
