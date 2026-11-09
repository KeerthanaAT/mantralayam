/* Guest-page i18n. English (locales/en.json) is the source. A key that is missing or still "TODO" in another language falls back to English.
   Markup: data-i18n="key" sets the text; data-i18n-attr="placeholder:key;aria-label:key" sets attributes. Script: t("key", { name, count }).
   Plurals: with { count }, t() tries key_<one|few|many|other> for the language, then key_other, then key. */
"use strict";
(() => {
  const LANGS = { en: "English", te: "తెలుగు", kn: "ಕನ್ನಡ", ta: "தமிழ்", hi: "हिन्दी" };
  let lang = "en", en = {}, cur = {};
  const todo = (v) => typeof v !== "string" || /^TODO\b/.test(v);
  const get = (k) => (!todo(cur[k]) ? cur[k] : !todo(en[k]) ? en[k] : null);
  const store = (v) => { try { return v === undefined ? localStorage.getItem("lang") : localStorage.setItem("lang", v); } catch { return null; } };
  const load = async (l) => { const r = await fetch(`/locales/${l}.json`); if (!r.ok) throw new Error("locale " + l); return r.json(); };

  function t(key, vars = {}) {
    const keys = vars.count != null ? [`${key}_${new Intl.PluralRules(lang).select(vars.count)}`, `${key}_other`, key] : [key];
    const s = keys.map(get).find((x) => x != null) ?? key;
    return s.replace(/\{(\w+)\}/g, (m, n) => (n in vars ? vars[n] : m));
  }

  function apply(root = document) {
    document.documentElement.lang = lang;
    root.querySelectorAll("[data-i18n]").forEach((e) => { e.textContent = t(e.dataset.i18n); });
    root.querySelectorAll("[data-i18n-attr]").forEach((e) => e.dataset.i18nAttr.split(";").forEach((p) => { const [a, k] = p.split(":"); e.setAttribute(a, t(k)); }));
    const sel = document.getElementById("lang");
    if (sel) sel.value = lang;
  }

  async function setLang(l) {
    if (!LANGS[l]) l = "en";
    cur = l === "en" ? en : await load(l).catch(() => ({}));  // a missing file just means English
    lang = l; store(l); apply();
    document.dispatchEvent(new CustomEvent("langchange", { detail: l }));
  }

  async function init() {
    en = await load("en").catch(() => ({}));
    const want = store() || (navigator.languages || []).map((x) => x.slice(0, 2)).find((x) => LANGS[x]) || "en";
    const sel = document.getElementById("lang");
    if (sel) {
      sel.replaceChildren(...Object.entries(LANGS).map(([k, n]) => Object.assign(document.createElement("option"), { value: k, textContent: n, lang: k })));
      sel.addEventListener("change", () => setLang(sel.value));
    }
    await setLang(want);
  }

  const has = (k) => get(k) != null;
  const source = (prefix) => Object.entries(en).filter(([k]) => k.startsWith(prefix));  // English templates, used to recognise the server's English messages
  window.I18N = { t, has, source, init, setLang, apply, get lang() { return lang; }, LANGS };
  window.t = t;
})();
