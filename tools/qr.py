"""QR Code encoder (standard library only): byte mode, error correction level M, versions 1 to 10 (up to 213 bytes).

qr_matrix(text)      -> list of rows of bools (True = dark module), without the quiet zone
qr_png(matrix, ...)  -> PNG bytes (grayscale, with the quiet zone), for email
Follows ISO/IEC 18004; the layout and masking steps are the standard ones, so any phone or scanner can read the result.
"""
import struct
import zlib

ECC_PER_BLOCK = (None, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)  # level M, index = version
NUM_BLOCKS = (None, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
MAX_VERSION = 10


def _raw_modules(ver):
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        a = ver // 7 + 2
        n -= (25 * a - 10) * a - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - ECC_PER_BLOCK[ver] * NUM_BLOCKS[ver]


def _gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    out = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            out[j] = _gf_mul(out[j], root) ^ (out[j + 1] if j + 1 < degree else 0)
        root = _gf_mul(root, 2)
    return out


def _rs_remainder(data, divisor):
    out = [0] * len(divisor)
    for b in data:
        factor = b ^ out.pop(0)
        out.append(0)
        for i, c in enumerate(divisor):
            out[i] ^= _gf_mul(c, factor)
    return out


def _codewords(data, ver):
    """Splits the data codewords into blocks, adds the error correction to each and interleaves them."""
    nb, ecc = NUM_BLOCKS[ver], ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    short_blocks = nb - raw % nb
    short_len = raw // nb
    div, blocks, k = _rs_divisor(ecc), [], 0
    for i in range(nb):
        size = short_len - ecc + (0 if i < short_blocks else 1)
        chunk = data[k:k + size]
        k += size
        blocks.append(chunk + [None] * (1 if i < short_blocks else 0) + _rs_remainder(chunk, div))
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc or j >= short_blocks:
                out.append(blk[i])
    return out


def _alignment_positions(ver):
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
    return [6] + [ver * 4 + 10 - i * step for i in range(n - 2, -1, -1)]


class _Grid:
    def __init__(self, ver):
        self.ver, self.size = ver, ver * 4 + 17
        self.m = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]
        for i in range(self.size):
            self.put(6, i, i % 2 == 0)
            self.put(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (self.size - 4, 3), (3, self.size - 4)):
            self.finder(cx, cy)
        pos = _alignment_positions(ver)
        for i, a in enumerate(pos):
            for j, b in enumerate(pos):
                if not ((i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0)):
                    self.align(a, b)
        self.format_bits(0)
        self.version_bits()

    def put(self, x, y, dark):
        self.m[y][x] = dark
        self.fn[y][x] = True

    def finder(self, cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < self.size and 0 <= y < self.size:
                    self.put(x, y, max(abs(dx), abs(dy)) not in (2, 4))

    def align(self, cx, cy):
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                self.put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

    def format_bits(self, mask):
        data = (0 << 3) | mask  # level M is 00
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        bit = lambda i: (bits >> i) & 1 == 1
        for i in range(6):
            self.put(8, i, bit(i))
        self.put(8, 7, bit(6))
        self.put(8, 8, bit(7))
        self.put(7, 8, bit(8))
        for i in range(9, 15):
            self.put(14 - i, 8, bit(i))
        for i in range(8):
            self.put(self.size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.put(8, self.size - 15 + i, bit(i))
        self.put(8, self.size - 8, True)

    def version_bits(self):
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (self.ver << 12) | rem
        for i in range(18):
            b = (bits >> i) & 1 == 1
            a, c = self.size - 11 + i % 3, i // 3
            self.put(a, c, b)
            self.put(c, a, b)

    def place(self, data):
        i = 0
        for right in range(self.size - 1, 0, -2):
            if right == 6:
                right = 5
            for vert in range(self.size):
                for j in range(2):
                    x = right - j
                    y = self.size - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not self.fn[y][x] and i < len(data) * 8:
                        self.m[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1

    def mask(self, k):
        f = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0, lambda x, y: (x + y) % 3 == 0,
             lambda x, y: (x // 3 + y // 2) % 2 == 0, lambda x, y: x * y % 2 + x * y % 3 == 0,
             lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0, lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)[k]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y):
                    self.m[y][x] = not self.m[y][x]

    def penalty(self):
        n, m, score = self.size, self.m, 0
        lines = [row for row in m] + [[m[y][x] for y in range(n)] for x in range(n)]
        for line in lines:
            run = 1
            for i in range(1, n):
                run = run + 1 if line[i] == line[i - 1] else 1
                if run == 5:
                    score += 3
                elif run > 5:
                    score += 1
            s = "".join("1" if v else "0" for v in line)
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
        for y in range(n - 1):
            for x in range(n - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(map(sum, m))
        return score + ((abs(dark * 20 - n * n * 10) + n * n - 1) // (n * n) - 1) * 10


def qr_matrix(text):
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    for ver in range(1, MAX_VERSION + 1):
        count_bits = 8 if ver < 10 else 16
        if 4 + count_bits + 8 * len(data) <= _data_codewords(ver) * 8:
            break
    else:
        raise ValueError("text too long for a QR code of this encoder (max 213 bytes)")
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in range(count_bits - 1, -1, -1)] + [(b >> i) & 1 for b in data for i in range(7, -1, -1)]
    cap = _data_codewords(ver) * 8
    bits += [0] * min(4, cap - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0
    while len(words) < cap // 8:
        words.append((0xEC, 0x11)[pad % 2])
        pad += 1
    grid = _Grid(ver)
    grid.place(_codewords(words, ver))
    best, best_score = 0, None
    for k in range(8):
        grid.mask(k)
        grid.format_bits(k)
        score = grid.penalty()
        grid.mask(k)  # undo
        if best_score is None or score < best_score:
            best, best_score = k, score
    grid.mask(best)
    grid.format_bits(best)
    return grid.m


def qr_png(matrix, scale=6, border=4):
    n = len(matrix)
    side = (n + 2 * border) * scale
    rows = []
    for y in range(side):
        my = y // scale - border
        line = bytearray(b"\xff" * side)
        if 0 <= my < n:
            for mx, dark in enumerate(matrix[my]):
                if dark:
                    x0 = (mx + border) * scale
                    line[x0:x0 + scale] = b"\x00" * scale
        rows.append(b"\x00" + bytes(line))

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")
