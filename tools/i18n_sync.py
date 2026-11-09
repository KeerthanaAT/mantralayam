"""Keeps web/locales/<lang>.json in step with the English source (web/locales/en.json).

Usage: python tools/i18n_sync.py
Adds every missing key to te, kn, ta and hi with the value "TODO" (the guest page shows English for those), drops keys that no longer exist in
English, keeps real translations untouched, and prints how many keys each language still has to translate.
"""
import json
from pathlib import Path

LOCALES = Path(__file__).resolve().parent.parent / "web" / "locales"
OTHERS = ("te", "kn", "ta", "hi")


def sync():
    en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
    counts = {}
    for lang in OTHERS:
        path = LOCALES / f"{lang}.json"
        have = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        out = {k: have.get(k, "TODO") for k in en}
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        counts[lang] = sum(v.startswith("TODO") for v in out.values())
    return len(en), counts


if __name__ == "__main__":
    total, todo = sync()
    for lang, n in todo.items():
        print(f"{lang}: {total - n}/{total} translated, {n} TODO")
