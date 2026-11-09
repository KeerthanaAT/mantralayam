"""Minimal PDF writer (standard library only): one or more A4 pages with text in Helvetica, rules, and filled squares (used to draw a QR code as vector).

Text is Western (WinAnsi): characters outside it print as "?". Indian-language names therefore cannot appear in the PDF, which is why booking receipts as PDF are in English;
the printable HTML receipt and the email carry the guest's own language.
"""
W, H = 595, 842  # A4 in points


def _esc(s):
    return s.encode("cp1252", "replace").decode("cp1252").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Pdf:
    def __init__(self, title=""):
        self.title, self.pages = title, [[]]

    def _add(self, op):
        self.pages[-1].append(op)

    def new_page(self):
        self.pages.append([])

    def text(self, x, y, s, size=11, bold=False, grey=0.0, right=False):
        """y counts from the top of the page. right=True ends the text at x."""
        s = str(s)
        if right:
            x -= len(s) * size * 0.5  # Helvetica averages about half the font size per character; good enough for right-aligned numbers
        self._add(f"BT /{'F2' if bold else 'F1'} {size} Tf {grey} g {x:.2f} {H - y:.2f} Td ({_esc(s)}) Tj ET")

    def rule(self, x1, x2, y, width=0.5, grey=0.7):
        self._add(f"{grey} G {width} w {x1:.2f} {H - y:.2f} m {x2:.2f} {H - y:.2f} l S")

    def squares(self, matrix, x, y, size, quiet=2):
        """Draws a boolean matrix (a QR code) with its top-left at (x, y), `size` points wide including a quiet zone of `quiet` modules."""
        n = len(matrix)
        u = size / (n + 2 * quiet)
        self._add(f"1 g {x:.2f} {H - y - size:.2f} {size:.2f} {size:.2f} re f 0 g")
        for r, row in enumerate(matrix):
            c = 0
            while c < n:  # one rectangle per horizontal run keeps the file small
                if row[c]:
                    start = c
                    while c < n and row[c]:
                        c += 1
                    self._add(f"{x + (start + quiet) * u:.2f} {H - y - (r + quiet + 1) * u:.2f} {(c - start) * u + 0.15:.2f} {u + 0.15:.2f} re f")
                else:
                    c += 1

    def render(self):
        objs = [b"<< /Type /Catalog /Pages 2 0 R >>", None, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]
        kids = []
        for ops in self.pages:
            body = "\n".join(ops).encode("cp1252")
            objs.append(b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream")
            objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {len(objs)} 0 R >>".encode())
            kids.append(f"{len(objs)} 0 R")
        objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
        objs.append(f"<< /Title ({_esc(self.title)}) /Producer (Mantralayam Rooms) >>".encode())
        out, offsets = bytearray(b"%PDF-1.4\n"), []
        for i, o in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {len(objs)} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)
