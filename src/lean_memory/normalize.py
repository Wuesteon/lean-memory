"""The tree's ONE value-preserving text normalization.

Two independent subsystems need "are these two strings the same value written
differently?": WP10a's DEDUP-EXACT clustering over `fact_text`
(`maintain/transforms.py`) and WP15's entity identity over `entity.name`
(`store/sqlite_store.py`). They must not answer it differently, so the function
lives here — a neutral module neither of them owns — and `maintain.transforms`
re-exports it so WP10a's import path and tests are unchanged.

Not in scope for this module: `extract/router.py`'s `_norm` (`strip().lower()`
+ whitespace collapse). That is a coref *heuristic* over pronouns, not an
identity decision, and deliberately stays local to the router.
"""

from __future__ import annotations

import unicodedata

__all__ = ["normalize_text", "entity_key"]


def normalize_text(s: str) -> str:
    """Value-PRESERVING normalization: NFC + case-fold + whitespace collapse.

    NFC (canonical Unicode composition) + case-fold + whitespace collapse — and
    NOTHING else. Never stemming, never synonyms: a lossy normalization could merge
    genuinely distinct values ('salary 100k' vs 'salary 110k', 'likes jazz' vs
    'likes blues') — the verified risk that makes DEDUP-EXACT safe to auto-apply
    (§4.1). Two texts share a normal form iff they are the same value written
    differently (case / spacing / Unicode form).

    `str.casefold()` is a FULL Unicode case fold, not an ASCII map — which is the
    whole reason entity collation uses this rather than SQLite's `NOCASE`
    collation: `Café`/`CAFÉ` and `ЖУК`/`жук` fold here and do not under NOCASE.

    Deliberately NOT done, and their consequences:
      - no punctuation stripping   → `Yahoo!` ≠ `Yahoo`, `Acme.` ≠ `Acme`
        (unlike `Memory._restatement_key`, which strips EDGE punctuation off
        fact *text*; a name is not a sentence — stripping would kill `Yahoo!`);
      - no diacritic folding       → `Café` ≠ `Cafe` (that is transliteration);
      - NFC, not NFKC              → fullwidth `ＡＣＭＥ` ≠ `ACME`. NFKC is a
        *compatibility* fold (`ﬁ`→`fi`, `½`→`1⁄2`), which is lossier than "the
        same value written differently";
      - no locale tailoring        → Turkish dotted `İ` does not fold to `i`
        (`'İ'.casefold()` is `i` + U+0307), and German `Weiß` DOES fold onto
        `Weiss`. Both are locale problems no locale-independent fold can solve,
        and picking a locale would break determinism.
    """
    nfc = unicodedata.normalize("NFC", s)
    folded = nfc.casefold()
    return " ".join(folded.split())


#: Entity identity (`entity.name_key`) uses the SAME definition as DEDUP-EXACT —
#: an alias, not a copy, so the two can never drift. Read it at the call site as
#: "this string is being used as an entity key", not as a second policy.
entity_key = normalize_text
