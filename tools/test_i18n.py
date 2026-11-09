"""Self-check for the i18n layer: every locale has the same keys as English, placeholders match, and every key used in the markup exists.
Run: python tools/test_i18n.py   (or pytest)
"""
import ast
import itertools
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "web" / "locales"
LANGS = ("en", "te", "kn", "ta", "hi")
load = lambda lang: json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
holes = lambda s: set(re.findall(r"\{(\w+)\}", s))
DYNAMIC = ("err.", "status.", "id.", "calltime.", "sms.", "receipt.", "email.", "notice.weather", "notice.river", "notice.general", "sub.confirm_subject", "sub.confirm_text", "sub.stop")  # looked up by name at run time: from the server message, the booking status, the ID type list, the call-time option; sms.* and receipt.* are used by the server (SMS and receipt text)


def samples(node):
    """Every English text a UserError/throttle/to_int argument can produce, with each {placeholder} replaced by 1 (a plural 's' either present or absent). [] for variables."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return samples(node.body) + samples(node.orelse)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return ["".join(p) for p in itertools.product(samples(node.left) or [""], samples(node.right) or [""])] if samples(node.left) and samples(node.right) else []
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append([v.value])
            elif isinstance(v.value, ast.IfExp):
                parts.append(samples(v.value.body) + samples(v.value.orelse) or ["1"])
            else:
                parts.append(["1"])
        if parts and parts[0] == ["1"]:  # f"{msg} Please try again in ...": the base message is checked on its own
            return []
        return ["".join(p) for p in itertools.product(*parts)]
    return []


def server_messages():
    tree = ast.parse((ROOT / "tools" / "mantralayam_rooms.py").read_text(encoding="utf-8"))
    skip = {f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name in ("site_check",)}  # admin-only messages
    out = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name not in skip:
            for call in ast.walk(fn):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    name, args = call.func.id, call.args
                    arg = args[0] if name == "UserError" and args else args[3] if name == "throttle" and len(args) > 3 else args[1] if name == "to_int" and len(args) > 1 else None
                    if arg is not None:
                        out.append((fn.name, samples(arg)))
    return [(f, texts) for f, texts in out if texts]


def test_locales_match_english():
    en = load("en")
    assert en and all(v.strip() and not v.startswith("TODO") for v in en.values()), "English is the source: no empty or TODO values"
    for lang in LANGS[1:]:
        d = load(lang)
        assert list(d) == list(en), f"{lang}: keys differ from en.json, run python tools/i18n_sync.py"
        for k, v in d.items():
            assert v.startswith("TODO") or holes(v) == holes(en[k]), f"{lang}.{k}: placeholders differ from English"


def test_markup_keys_exist():
    en = load("en")
    html = (ROOT / "web" / "guest.html").read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n="([\w.]+)"', html)) | set(re.findall(r'data-i18n-attr="[^"]*?:([\w.]+)', html)) | set(re.findall(r'\bt\("([\w.]*\w)"', html))
    missing = {k for k in used if not k.endswith("_") and k not in en and k + "_other" not in en}  # a name ending in _ is built at run time  # a plural base key is present when key_other exists
    assert used and not missing, sorted(missing)
    unused = [k for k in en if not k.startswith(DYNAMIC) and not re.search(rf"(?<![\w.]){re.escape(re.sub(r'_(one|other)$', '', k))}(?![\w.])", html)]  # a key nobody references is a leftover or a typo
    assert not unused, unused


def template_regex(tpl):
    """The same matching the guest page does: {name} in an English err.* template stands for any text."""
    return re.compile("^" + re.sub(r"\\{(\w+)\\}", lambda m: f"(?P<{m[1]}>.+?)", re.escape(tpl)) + "$")



def test_server_messages_have_translations():
    """The guest page recognises the API's English errors by the err.* templates in en.json: every guest-facing message must match one."""
    en = load("en")
    templates = [template_regex(v) for k, v in en.items() if k.startswith("err.") and not k.startswith("err.retry_")]
    found = server_messages()
    assert len(found) > 40, len(found)
    for fn, texts in found:
        assert any(t.match(x) for t in templates for x in texts), (fn, texts)


if __name__ == "__main__":
    test_locales_match_english()
    test_markup_keys_exist()
    test_server_messages_have_translations()
    print("ok")
