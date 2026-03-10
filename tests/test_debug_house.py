import re, sys
sys.stdout.reconfigure(encoding="utf-8")
from rapidfuzz import fuzz

_SYNONYMS = [
    ("democratic party", "democrats"), ("democrat party", "democrats"),
    ("republican party", "republicans"), ("gop", "republicans"),
    ("united states", "us"), ("u s ", "us "),
    ("control the senate", "win the senate"), ("control the house", "win the house"),
]

def _clean(text):
    return re.sub(r"[^a-zA-Z0-9 ]", " ", text).strip()

def _normalize_market(text):
    t = text.lower()
    for old, new in _SYNONYMS:
        t = t.replace(old, new)
    return _clean(t)

poly_markets = [
    "Will the Democratic Party control the House after the 2026 midterms?",
    "Will the Republican Party control the House after the 2026 midterms?",
    "Will another party control the House after the 2026 midterms?",
]
kalshi_markets = [
    "Will Democrats win the House in 2026?",
    "Will Republicans win the House in 2026?",
]

for p in poly_markets:
    p_raw = _clean(p)
    p_norm = _normalize_market(p)
    print(f"\nPOLY: {p[:60]}")
    print(f"  raw:  {p_raw}")
    print(f"  norm: {p_norm}")
    for k in kalshi_markets:
        k_raw = _clean(k)
        k_norm = _normalize_market(k)
        s_tsr = fuzz.token_set_ratio(p_raw, k_raw)
        s_tsor = fuzz.token_sort_ratio(p_raw, k_raw)
        s_n_tsr = fuzz.token_set_ratio(p_norm, k_norm)
        s_n_tsor = fuzz.token_sort_ratio(p_norm, k_norm)
        s_strict = fuzz.ratio(p_norm, k_norm)
        s = max(s_tsr, s_tsor, s_n_tsr, s_n_tsor, int(s_strict * 0.9))
        print(f"  vs KALSHI: {k[:50]}")
        print(f"    raw tsr={s_tsr} tsor={s_tsor} | norm tsr={s_n_tsr} tsor={s_n_tsor} strict={s_strict:.0f} | FINAL={s}")
