"""Normalization (design.md sections 5.1, 5.5, 16.1).

Normalization is where a whole class of false merges is created quietly.  The
governing example is email: Gmail ignores dots in the local part, most other
providers do not.  Strip dots globally and you will merge
``j.smith@company.com`` with ``jsmith@company.com`` at a domain where those are
two different people -- and nobody will trace it back for months.

Every function here is idempotent: ``f(f(x)) == f(x)`` (section 17.3).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from lep.core.types import Record

GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}

#: Domains that also ignore dots in the local part.  Keep this list short and
#: evidence-based; guessing here creates false merges.
DOT_INSENSITIVE_DOMAINS = GMAIL_DOMAINS

#: Company suffix variants collapse to nothing (section 5.5).
COMPANY_SUFFIXES = {
    "inc", "inc.", "incorporated", "llc", "l.l.c.", "ltd", "ltd.", "limited",
    "plc", "corp", "corp.", "corporation", "co", "co.", "company", "gmbh",
    "ag", "sa", "s.a.", "nv", "n.v.", "bv", "b.v.", "ab", "as", "oy", "kk",
    "k.k.", "pty", "pte", "srl", "s.r.l.", "spa", "s.p.a.", "sas", "holdings",
    "group", "the",
}

#: Deliberately small.  A nickname table is a source of false merges when it is
#: ambiguous ("Alex" is Alexander or Alexandra), so it is used as *evidence*
#: for the scorer, never as a deterministic rule.
NICKNAMES: dict[str, str] = {
    "bill": "william", "will": "william", "billy": "william",
    "bob": "robert", "rob": "robert", "bobby": "robert",
    "dick": "richard", "rick": "richard", "ricky": "richard",
    "jim": "james", "jimmy": "james", "jamie": "james",
    "joe": "joseph", "joey": "joseph",
    "mike": "michael", "mick": "michael",
    "dave": "david",
    "steve": "stephen", "steven": "stephen",
    "tom": "thomas", "tommy": "thomas",
    "tony": "anthony",
    "chris": "christopher",
    "dan": "daniel", "danny": "daniel",
    "matt": "matthew",
    "nick": "nicholas",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine",
    "liz": "elizabeth", "beth": "elizabeth", "betty": "elizabeth",
    "sue": "susan", "suzy": "susan",
    "peggy": "margaret", "maggie": "margaret",
    "jenny": "jennifer", "jen": "jennifer",
}

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def strip_accents(text: str) -> str:
    """Fold accents so ``Müller`` and ``Muller`` compare equal."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_email(email: str | None) -> str | None:
    """Provider-aware email normalization (section 16.1).

    Gmail ignores dots and ``+`` tags.  Most other providers do NOT ignore
    dots, so normalizing globally would merge distinct people.  Tag stripping
    is broadly safe; dot stripping is not.
    """
    if not email:
        return None
    email = strip_accents(email.strip().lower())
    if "@" not in email:
        return email or None
    local, domain = email.rsplit("@", 1)
    domain = domain.strip(".")
    if not local or not domain:
        return None

    if domain in DOT_INSENSITIVE_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        if domain in GMAIL_DOMAINS:
            domain = "gmail.com"
    else:
        # +tag stripping is broadly safe; dot stripping is NOT.
        local = local.split("+", 1)[0]

    return f"{local}@{domain}" if local else None


def email_domain(email: str | None) -> str | None:
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        return None
    return normalized.rsplit("@", 1)[1]


#: Free-mail domains are useless as a blocking key on their own and weak as
#: evidence: everybody shares them (section 5.2).
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "aol.com", "icloud.com", "me.com", "protonmail.com", "gmx.de",
    "mail.com", "yandex.ru", "qq.com", "163.com",
}


def is_freemail(domain: str | None) -> bool:
    return bool(domain) and domain in FREEMAIL_DOMAINS


def normalize_phone(phone: str | None, default_country: str = "1") -> str | None:
    """Best-effort E.164.

    Deliberately conservative: a number that cannot be interpreted with
    confidence returns None rather than a plausible-looking wrong answer,
    because phone is used as a high-precision deterministic key (section 5.1).
    """
    if not phone:
        return None
    raw = phone.strip()
    has_plus = raw.startswith("+") or raw.startswith("00")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    if not digits:
        return None
    # Drop obvious extensions: "555-0100 x1234"
    if re.search(r"(?:ext|x|extension)\.?\s*\d+$", raw, re.IGNORECASE):
        digits = re.sub(r"\D", "", re.split(r"(?:ext|x|extension)\.?", raw, flags=re.IGNORECASE)[0])
    if has_plus:
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 10:
        return f"+{default_country}{digits}"
    if len(digits) == 11 and digits.startswith(default_country):
        return f"+{digits}"
    if 8 <= len(digits) <= 15:
        return f"+{digits}"
    return None


def normalize_name(name: str | None) -> str | None:
    """Lowercase, accent-folded, punctuation-stripped."""
    if not name:
        return None
    text = strip_accents(name.strip().lower())
    text = text.replace("'", "").replace("`", "")
    text = _NON_ALNUM.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return text or None


def canonical_first_name(name: str | None) -> str | None:
    """Map a known nickname to its formal form.

    Used as a comparison level for the scorer, never as a merge rule.
    """
    normalized = normalize_name(name)
    if not normalized:
        return None
    head = normalized.split(" ")[0]
    return NICKNAMES.get(head, head)


def normalize_company(name: str | None) -> str | None:
    """Strip legal suffixes and punctuation (section 5.5).

    "Alphabet Inc." and "Alphabet" collapse; "Alphabet" and "Google" do not,
    and no amount of string normalization will fix that -- domain is the
    stronger signal, and sometimes the answer is genuinely ambiguous.
    """
    normalized = normalize_name(name)
    if not normalized:
        return None
    parts = [p for p in normalized.split(" ") if p]
    while parts and parts[-1] in COMPANY_SUFFIXES:
        parts.pop()
    while parts and parts[0] in {"the"}:
        parts.pop(0)
    return " ".join(parts) or normalized


def normalize_domain(value: str | None) -> str | None:
    """Host part of a URL or bare domain, without ``www.``."""
    if not value:
        return None
    text = value.strip().lower()
    text = re.sub(r"^[a-z]+://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0]
    text = text.removeprefix("www.")
    text = text.strip(".")
    return text or None


def normalize_linkedin(url: str | None) -> str | None:
    """LinkedIn profile slug -- a high-precision identifier (section 5.1)."""
    if not url:
        return None
    text = url.strip().lower().rstrip("/")
    match = re.search(r"linkedin\.com/(?:in|pub)/([^/?#]+)", text)
    if match:
        return match.group(1)
    return text or None


# --------------------------------------------------------------------------
# Double Metaphone
# --------------------------------------------------------------------------
#
# Section 5.2 asks for Double Metaphone rather than Soundex, because it handles
# non-English names materially better -- which matters for real contact data.
#
# This is a compact implementation of the common rules (Philips, 2000): Slavic,
# Germanic, Spanish, Italian and French handling for the letter groups that
# actually differ from English.  It is not the complete 1999 C reference.  For
# production, install the ``metaphone`` package and swap :func:`double_metaphone`
# for it; the tests in ``tests/test_normalize.py`` pin the behaviour this code
# relies on (variants of the same name agreeing, distinct names disagreeing).

_VOWELS = set("AEIOUY")


def _is_vowel(ch: str) -> bool:
    return ch in _VOWELS


def double_metaphone(name: str | None, max_length: int = 4) -> tuple[str, str]:
    """Return ``(primary, secondary)`` phonetic codes.

    Two names sharing either code are phonetically similar; this is used as a
    blocking key (``*_dm``) and as a comparison level, never on its own as a
    merge rule.
    """
    if not name:
        return ("", "")
    text = strip_accents(name).upper()
    text = re.sub(r"[^A-Z]", "", text)
    if not text:
        return ("", "")

    primary: list[str] = []
    secondary: list[str] = []
    pos = 0
    length = len(text)
    padded = text + "     "

    def add(p: str, s: str | None = None) -> None:
        primary.append(p)
        secondary.append(p if s is None else s)

    def at(i: int) -> str:
        return padded[i] if 0 <= i < length else ""

    def substr(i: int, n: int) -> str:
        if i < 0:
            return ""
        return padded[i : i + n]

    # Skip silent initial letters.
    if substr(0, 2) in ("GN", "KN", "PN", "WR", "PS"):
        pos = 1
    if at(0) == "X":                      # "Xavier" -> S
        add("S")
        pos = 1

    is_slavo_germanic = bool(re.search(r"W|K|CZ|WITZ", text))

    while pos < length and len(primary) < max_length:
        ch = at(pos)

        if _is_vowel(ch):
            if pos == 0:
                add("A")
            pos += 1
            continue

        if ch == "B":
            add("P")
            pos += 2 if at(pos + 1) == "B" else 1
        elif ch == "C":
            if substr(pos, 4) == "CHIA":
                add("K")
                pos += 2
            elif substr(pos, 2) == "CH":
                if pos == 0 and substr(pos, 6) in ("CHARAC", "CHARIS") or substr(pos + 2, 3) in ("ORE", "YMO"):
                    add("K")           # Greek roots: character, chorus
                elif substr(pos + 2, 2) in ("AE", "AO") or substr(pos, 4) == "CHUR":
                    add("K")
                else:
                    add("X", "K")      # "X" is the SH sound
                pos += 2
            elif substr(pos, 2) == "CZ":
                add("S", "X")
                pos += 2
            elif substr(pos, 3) == "CIA":
                add("X")
                pos += 3
            elif substr(pos, 2) in ("CI", "CE", "CY"):
                add("S")
                pos += 2
            elif substr(pos, 2) == "CK":
                add("K")
                pos += 2
            elif substr(pos, 2) == "CC" and not substr(pos + 2, 1) in ("I", "E", "Y"):
                add("K")
                pos += 2
            else:
                add("K")
                pos += 1
        elif ch == "D":
            if substr(pos, 2) == "DG":
                if at(pos + 2) in ("I", "E", "Y"):
                    add("J")
                    pos += 3
                else:
                    add("TK")
                    pos += 2
            elif substr(pos, 2) in ("DT", "DD"):
                add("T")
                pos += 2
            else:
                add("T")
                pos += 1
        elif ch == "F":
            add("F")
            pos += 2 if at(pos + 1) == "F" else 1
        elif ch == "G":
            if at(pos + 1) == "H":
                if pos > 0 and not _is_vowel(at(pos - 1)):
                    add("K")
                elif pos == 0:
                    add("K")
                else:
                    pass               # silent: "night", "Hough"
                pos += 2
            elif at(pos + 1) == "N":
                # "gn" -> N, but Italian "Magnani" keeps a KN secondary.
                if pos == 0 or is_slavo_germanic:
                    add("KN", "N")
                else:
                    add("N", "KN")
                pos += 2
            elif substr(pos, 2) in ("GI", "GE", "GY"):
                add("J", "K")
                pos += 2
            else:
                add("K")
                pos += 2 if at(pos + 1) == "G" else 1
        elif ch == "H":
            # Only pronounced between a vowel and a following vowel.
            if (pos == 0 or _is_vowel(at(pos - 1))) and _is_vowel(at(pos + 1)):
                add("H")
            pos += 1
        elif ch == "J":
            if substr(pos, 4) == "JOSE" or text.startswith("SAN "):
                add("H")               # Spanish
            elif pos == 0:
                add("J", "A")
            else:
                add("J", "H")
            pos += 2 if at(pos + 1) == "J" else 1
        elif ch == "K":
            add("K")
            pos += 2 if at(pos + 1) == "K" else 1
        elif ch == "L":
            add("L")
            pos += 2 if at(pos + 1) == "L" else 1
        elif ch == "M":
            add("M")
            pos += 2 if at(pos + 1) == "M" else 1
        elif ch == "N":
            add("N")
            pos += 2 if at(pos + 1) == "N" else 1
        elif ch == "P":
            if at(pos + 1) == "H":
                add("F")
                pos += 2
            else:
                add("P")
                pos += 2 if at(pos + 1) in ("P", "B") else 1
        elif ch == "Q":
            add("K")
            pos += 2 if at(pos + 1) == "Q" else 1
        elif ch == "R":
            add("R")
            pos += 2 if at(pos + 1) == "R" else 1
        elif ch == "S":
            if substr(pos, 3) == "SCH":
                if substr(pos + 3, 2) in ("ER", "EN", "UY", "ED", "EM"):
                    add("X", "SK")     # Germanic
                else:
                    add("X")
                pos += 3
            elif substr(pos, 2) in ("SH", "SI", "SY"):
                add("X" if substr(pos, 2) == "SH" else "S")
                pos += 2
            else:
                add("S")
                pos += 2 if at(pos + 1) in ("S", "Z") else 1
        elif ch == "T":
            if substr(pos, 3) in ("TIA", "TIO"):
                add("X")
                pos += 3
            elif substr(pos, 2) == "TH":
                add("0", "T")          # "0" is the TH sound
                pos += 2
            elif substr(pos, 2) == "TC":
                pos += 1
            else:
                add("T")
                pos += 2 if at(pos + 1) == "T" else 1
        elif ch == "V":
            add("F")
            pos += 2 if at(pos + 1) == "V" else 1
        elif ch == "W":
            if substr(pos, 2) == "WR":
                add("R")
                pos += 2
            elif pos == 0 and (_is_vowel(at(pos + 1)) or substr(pos, 2) == "WH"):
                # "Wasserman" sounds like a vowel start; "Wagner" (Slavic or
                # Germanic) keeps an F secondary.
                add("A", "F" if _is_vowel(at(pos + 1)) else "A")
                pos += 1
            elif is_slavo_germanic and pos > 0 and _is_vowel(at(pos - 1)):
                add("F")
                pos += 1
            else:
                pos += 1  # silent
        elif ch == "X":
            add("KS")
            pos += 2 if at(pos + 1) in ("C", "X") else 1
        elif ch == "Z":
            if at(pos + 1) == "H":
                add("J")
                pos += 2
            else:
                add("S", "TS")
                pos += 2 if at(pos + 1) == "Z" else 1
        else:  # pragma: no cover - non-letters already stripped
            pos += 1

    return (
        "".join(primary)[:max_length],
        "".join(secondary)[:max_length],
    )


def dm_primary(name: str | None) -> str:
    return double_metaphone(name)[0]


def dm_codes(name: str | None) -> set[str]:
    return {code for code in double_metaphone(name) if code}


def phonetically_equal(a: str | None, b: str | None) -> bool:
    """True when the two names share either phonetic code."""
    ca, cb = dm_codes(a), dm_codes(b)
    return bool(ca and cb and ca & cb)


# --------------------------------------------------------------------------


def normalize_record(record: Record, *, default_country: str = "1") -> Record:
    """Populate ``record.normalized`` with every derived key matching needs.

    Idempotent, so re-running normalization over a corpus is safe.
    """
    f = record.fields
    email = normalize_email(f.get("email"))
    first = normalize_name(f.get("first_name"))
    last = normalize_name(f.get("last_name"))
    company = normalize_company(f.get("company") or f.get("company_name") or f.get("name"))
    domain = normalize_domain(f.get("domain") or f.get("website"))
    phone = normalize_phone(f.get("phone"), default_country=default_country)

    record.normalized = {
        "email": email,
        "email_domain": email_domain(f.get("email")),
        "email_is_freemail": is_freemail(email_domain(f.get("email"))),
        "first_name": first,
        "first_name_canonical": canonical_first_name(f.get("first_name")),
        "last_name": last,
        "first_initial": (first or "")[:1] or None,
        "first_name_dm": dm_primary(first),
        "last_name_dm": dm_primary(last),
        "normalized_company": company,
        "company_dm": dm_primary(company),
        "domain": domain or (email_domain(f.get("email")) if not is_freemail(email_domain(f.get("email"))) else None),
        "phone_e164": phone,
        "linkedin": normalize_linkedin(f.get("linkedin") or f.get("linkedin_url")),
        "job_title": normalize_name(f.get("job_title") or f.get("title")),
    }
    return record


def normalize_all(records: Iterable[Record], **kwargs: Any) -> list[Record]:
    return [normalize_record(r, **kwargs) for r in records]
