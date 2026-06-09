"""Locale-tolerant money-string parsing, shared by every ingest path.

Handles both separator conventions — "1.234,56" (comma-decimal locales like
pt-BR) and "1,234.56" (dot-decimal locales) — plus currency prefixes
("R$ 50,00", "CAD$ 38.10", "US$ 5.99") and space grouping ("1 234,56").
"""

import math
import re
from typing import Optional

_CLEAN_RE = re.compile(r"[^\d.,\-]")


def parse_money(value, comma_decimal: Optional[bool] = None) -> Optional[float]:
    """Parse a money value to float. Returns None when unparseable — callers
    decide what a missing amount means (never silently 0.0).

    comma_decimal: True when ',' is known to be the decimal separator, False
    when '.' is, None to infer from the string alone. The hint only settles
    the ambiguous single-separator-with-3-digit-tail case ("1.234" / "1,234");
    a separator that can't be a grouping char (wrong tail length) is treated
    as the decimal mark regardless of the hint.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None

    s = _CLEAN_RE.sub("", str(value))
    negative = str(value).strip().startswith("(") and str(value).strip().endswith(")")
    if s.startswith("-"):
        negative = True
        s = s[1:]
    elif s.endswith("-"):
        negative = True
        s = s[:-1]
    if "-" in s or not any(c.isdigit() for c in s):
        return None

    if "." in s and "," in s:
        # The rightmost separator is the decimal mark.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "." in s or "," in s:
        sep = "." if "." in s else ","
        parts = s.split(sep)
        if len(parts) > 2:
            # Repeated separator → grouping ("1.234.567"), unless the last
            # group can't be one ("1.234.5").
            if len(parts[-1]) == 3:
                s = s.replace(sep, "")
            else:
                s = "".join(parts[:-1]) + "." + parts[-1]
        else:
            head, tail = parts
            if comma_decimal is None:
                # Money almost never has 3 decimals; "1,234"/"1.234" → 1234.
                sep_is_decimal = len(tail) != 3
            else:
                decimal_char = "," if comma_decimal else "."
                sep_is_decimal = sep == decimal_char or len(tail) != 3
            s = head + ("." if sep_is_decimal else "") + tail

    try:
        v = float(s)
    except ValueError:
        return None
    if not math.isfinite(v):
        return None
    return -v if negative else v
