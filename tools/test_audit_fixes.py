"""Regression tests for what the live audit (tools/audit_run.py) found. Each one fails on the code as it was before the fix.
Run: python tools/test_audit_fixes.py   (or pytest). The revenue-report regression is in test_reports.py.
"""
import json
import re
import shutil
import sys
from pathlib import Path

import test_staff_desk as td
import mantralayam_rooms as g

ROOT = Path(__file__).resolve().parent.parent
GUEST = (ROOT / "web" / "guest.html").read_text(encoding="utf-8")
TOKENS = (ROOT / "web" / "tokens.css").read_text(encoding="utf-8")
SHARED = (ROOT / "web" / "shared.css").read_text(encoding="utf-8")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200


def upload(user, tid, data):
    st, out, _ = td.s.dispatch("POST", f"/api/admin/room_types/{tid}/photo", None, {"_raw": data}, "127.0.0.1", td.TOKENS.get(user))
    return st, out


def test_room_photos():
    td.shutil.rmtree(g.UPLOADS, ignore_errors=True)
    assert upload("frontdesk", 2, PNG)[0] == 403 and upload("manager", 2, PNG)[0] == 403, "only the super admin changes photos"
    for bad in (b"GIF89a....", b"", b"RIFF\0\0\0\0WAVEfmt ", PNG[:5]):
        assert upload("admin", 2, bad)[0] == 400, bad
    assert upload("admin", 2, JPEG + b"\0" * 1_500_001)[0] == 413 and upload("admin", 999, PNG)[0] == 404
    st, r = upload("admin", 2, PNG)
    assert st == 200 and re.fullmatch(r"room-2-[0-9a-f]{8}\.png", r["photo"]) and (g.UPLOADS / r["photo"]).read_bytes() == PNG
    # the guest search carries it, the site serves it, nothing else in the folder is reachable
    info = g.dispatch("GET", "/api/public/search", dict(check_in=td.D(20), check_out=td.D(21), guests=2), None, "7.7.7.7")[1]
    photos = {t["room_type_id"]: t["photo"] for p in info["properties"] for t in p["types"]}
    assert photos[2] == r["photo"] and photos.get(1) is None
    assert g.read_upload(r["photo"]) == (PNG, "image/png")
    for name in ("../mantralayam.db", "..%2f..%2fx", "room-2-zz.png", "secret.txt", "room-2-12345678.png", "", None):
        assert g.read_upload(name) is None, name
    (g.UPLOADS / "secret.txt").write_text("x")
    assert g.read_upload("secret.txt") is None
    # a changed photo gets a new name and the old file goes; removing clears it
    st, r2 = upload("admin", 2, JPEG)
    assert st == 200 and r2["photo"] != r["photo"] and r2["photo"].endswith(".jpg") and not (g.UPLOADS / r["photo"]).exists()
    assert td.call("admin", "POST", "/api/admin/room_types/2/photo_remove", {})[0] == 200 and not (g.UPLOADS / r2["photo"]).exists()
    assert td.call("manager", "POST", "/api/admin/room_types/2/photo_remove", {})[0] == 403 and td.call("admin", "POST", "/api/admin/room_types/999/photo_remove", {})[0] == 404
    assert any(a == "admin.room_photo" for (a,) in td.db().execute("SELECT action FROM audit_log"))


def test_card_shows_a_photo_or_a_plain_picture_never_coming_soon():
    assert "rtphoto" in GUEST and "/uploads/" in GUEST and 'loading: "lazy"' in GUEST and "room.photo_alt" in GUEST
    assert "coming soon" not in GUEST.lower() and "coming soon" not in (ROOT / "web" / "locales" / "en.json").read_text(encoding="utf-8").lower()


def test_large_text_is_really_a_quarter_larger():
    """The page's base is 106.25% (17 px); the old 'large' mode was 125% (20 px), only 1.18x. It must be 1.25x the base."""
    base = float(re.search(r"^html \{ font-size: ([\d.]+)%", SHARED, re.M)[1])
    large = float(re.search(r'html\[data-text="large"\] \{ font-size: ([\d.]+)%', TOKENS)[1])
    assert abs(large / base - 1.25) < 0.001, (base, large)
    assert 'html[data-contrast="high"]' in TOKENS and "--text: #FFFFFF" in TOKENS


def test_accessibility_markup_found_by_axe():
    # WCAG 2.5.3 label in name: the accessible name must contain the visible text
    brand = re.search(r'<a class="brand"[^>]*>', GUEST)[0]
    assert "aria-label" not in brand, "an aria-label that omits the visible tagline made the link name mismatch"
    assert 'aria-label": `${t("room.book")}' in GUEST and "room.book_aria" not in GUEST, "the Book button's name must start with its visible words"
    for btn in re.findall(r'<button[^>]*id="a11y_(?:text|contrast)"[^>]*>.*?</button>', GUEST, re.S):
        assert "aria-label" not in btn.split(">")[0] and 'class="sr-only"' in btn and 'aria-hidden="true"' in btn, btn
    # a list may hold only list items: the display buttons sit in a div inside their li, and a landmark-less container has no aria-label
    assert '<li class="a11y"' not in GUEST and re.search(r'<li><div class="a11y" role="group"', GUEST)
    assert re.search(r'<div class="wrap" id="notices">', GUEST)
    # tap targets: a checkbox is tapped through its label, which must be 44 px tall
    assert re.search(r"^\.check1 \{ min-height: 44px;", SHARED, re.M)
    assert "icon-192.png" in re.search(r'<link rel="icon"[^>]*>', GUEST)[0], "the page asked the server for /favicon.ico, which does not exist (a 404 on every page load)"


def test_a11y_labels_exist_in_every_language():
    for lg in ("en", "te", "kn", "ta", "hi"):
        d = json.loads((ROOT / "web" / "locales" / f"{lg}.json").read_text(encoding="utf-8"))
        for k in ("a11y.group", "a11y.text", "a11y.contrast"):
            assert d[k] and not d[k].startswith("TODO"), (lg, k)


if __name__ == "__main__":
    try:
        td.setup()
        g.UPLOADS = td.TMP / "uploads"
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn) and not name.startswith("test_staff_desk"):
                try:
                    fn()
                except BaseException:
                    print("FAILED:", name, file=sys.stderr)
                    raise
        print("ok")
    finally:
        shutil.rmtree(td.TMP, ignore_errors=True)
