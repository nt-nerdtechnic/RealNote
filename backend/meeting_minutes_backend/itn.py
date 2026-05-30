"""ITN – Inverse Text Normalization for zh-TW ASR output.

Applied per-segment after ASR, before display/storage.
Converts spoken-form text to written/symbolic form:
  - Percentages  : 百分之三十  → 30%
  - Phone numbers: 零九一二三四五六七八 → 0912-345-678
  - Dates        : 二○二六年五月二十八日 → 2026年05月28日
  - Times        : 下午三點十五分 → 下午3:15
  - Numbers      : 一千五百 → 1,500 / 兩萬三千 → 23,000
"""
from __future__ import annotations

import re
from typing import Any

# ═══════════════════════════════════════════════════════════════════════════════
# Chinese numeral utilities
# ═══════════════════════════════════════════════════════════════════════════════

_DIGIT_MAP: dict[str, int] = {
    "零": 0, "○": 0, "〇": 0,
    "一": 1, "壹": 1,
    "二": 2, "貳": 2, "兩": 2,
    "三": 3, "參": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陸": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}

_UNIT_SMALL: dict[str, int] = {"十": 10, "百": 100, "千": 1000}
_UNIT_LARGE: dict[str, int] = {"萬": 10_000, "億": 100_000_000}

# Character class string fragments (used inside [...] in regex)
_D = "零○〇一二三四五六七八九兩壹貳參肆伍陸柒捌玖"  # digit chars only
_U = "十百千萬億"                                      # unit chars only
_N = _D + _U                                           # all numeral chars


def _parse_cn_num(s: str) -> int | None:
    """Parse a Chinese numeral string → int.  Returns None on unexpected chars.

    Handles: 十五=15, 一百=100, 三百二十五=325, 兩萬三千=23000, 一億兩千萬=120000000
    Year strings (一九九九) must be handled by the caller via per-digit mapping.
    """
    if not s:
        return None
    result = 0
    section = 0
    cur = 0
    at_start = True

    for ch in s:
        if ch in _DIGIT_MAP:
            cur = _DIGIT_MAP[ch]
            at_start = False
        elif ch in _UNIT_SMALL:
            mul = _UNIT_SMALL[ch]
            if ch == "十" and at_start:
                cur = 1          # 十五 → 1×10+5, not 0×10+5
            section += cur * mul
            cur = 0
            at_start = False
        elif ch in _UNIT_LARGE:
            section += cur
            result += section * _UNIT_LARGE[ch]
            section = 0
            cur = 0
            at_start = False
        else:
            return None          # unexpected char → bail

    return result + section + cur


def _cn_or_ascii_to_int(s: str) -> int | None:
    """Convert either ASCII digit string or Chinese numeral string → int."""
    if s.isdigit():
        return int(s)
    return _parse_cn_num(s)


# ═══════════════════════════════════════════════════════════════════════════════
# Percentage  百分之N → N%
# ═══════════════════════════════════════════════════════════════════════════════

# number part: Chinese numeral (with optional 點N decimal) or ASCII decimal
_NUM_PAT = (
    rf"[{_D}]+(?:[{_U}][{_N}]*)*(?:點[{_D}]+)?"  # CN: 三十 / 五點五
    rf"|\d+(?:\.\d+)?"                             # ASCII: 30 / 5.5
)

_PCT_RE = re.compile(rf"百分之(?P<num>{_NUM_PAT})")


def _replace_pct(m: re.Match) -> str:
    num_str = m.group("num")
    # Chinese decimal: 五點五 → 5.5
    if "點" in num_str:
        int_part, dec_part = num_str.split("點", 1)
        int_n = _cn_or_ascii_to_int(int_part) if int_part else 0
        dec_digits = "".join(str(_DIGIT_MAP.get(c, c)) for c in dec_part)
        return f"{int_n}.{dec_digits}%"
    n = _cn_or_ascii_to_int(num_str)
    if n is None:
        return m.group(0)
    return f"{n}%"


def _normalize_percentages(text: str) -> str:
    return _PCT_RE.sub(_replace_pct, text)


# ═══════════════════════════════════════════════════════════════════════════════
# Phone numbers  零九一二三四五六七八 → 0912-345-678
# Detects 10–11 digit runs (Chinese or ASCII) starting with 零/0
# ═══════════════════════════════════════════════════════════════════════════════

_PHONE_DIGIT = rf"[{_D}\d]"  # one digit (Chinese or ASCII)
_PHONE_RE = re.compile(rf"(?:零|0){_PHONE_DIGIT}{{9,10}}")


def _to_ascii_digits(s: str) -> str:
    """Convert a mixed Chinese/ASCII digit string to pure ASCII digits."""
    return "".join(
        str(_DIGIT_MAP[c]) if c in _DIGIT_MAP else c
        for c in s
        if c in _DIGIT_MAP or c.isdigit()
    )


def _fmt_phone(digits: str) -> str:
    """Format pure-digit string as Taiwan phone number."""
    n = len(digits)
    if n == 10:
        if digits.startswith("09"):
            # Mobile: 0912-345-678
            return f"{digits[:4]}-{digits[4:7]}-{digits[7:]}"
        # Landline: 02-1234-5678 (Taipei) or 03-123-4567 (other)
        area = 2 if digits[1] == "2" else 2
        return f"{digits[:area]}-{digits[area:area+4]}-{digits[area+4:]}"
    if n == 11:
        # 11-digit (international or special)
        return f"{digits[:4]}-{digits[4:8]}-{digits[8:]}"
    return digits


def _replace_phone(m: re.Match) -> str:
    digits = _to_ascii_digits(m.group(0))
    return _fmt_phone(digits)


def _normalize_phones(text: str) -> str:
    return _PHONE_RE.sub(_replace_phone, text)


# ═══════════════════════════════════════════════════════════════════════════════
# Dates  二○二六年五月二十八日 → 2026年05月28日
# ═══════════════════════════════════════════════════════════════════════════════

# Year: 4 ASCII digits OR 4 CN digit chars (digit-by-digit, e.g. 二○二六)
_YEAR_PAT = rf"(?:\d{{4}}|[{_D}]{{4}})"

# Month/day component: 1–2 ASCII digits, or CN numeral with optional 十
_MD_PAT = (
    rf"(?:\d{{1,2}}"
    rf"|十[{_D}]?"        # 十、十一…十九
    rf"|[{_D}]十[{_D}]?"  # 二十、三十…
    rf"|[{_D}])"           # 一…九
)

_DATE_YMD_RE = re.compile(
    rf"(?P<year>{_YEAR_PAT})年"
    rf"(?P<month>{_MD_PAT})月"
    rf"(?P<day>{_MD_PAT})[日號]?"
)
_DATE_MD_RE = re.compile(
    rf"(?P<month>{_MD_PAT})月"
    rf"(?P<day>{_MD_PAT})[日號]"
)


def _norm_year(s: str) -> str:
    """Convert year string: digit-by-digit mapping (一九九九 → 1999)."""
    if s.isdigit():
        return s
    return "".join(str(_DIGIT_MAP.get(c, c)) for c in s)


def _norm_md(s: str) -> str:
    """Convert month/day component to ASCII digit string."""
    if s.isdigit():
        return s
    n = _parse_cn_num(s)
    return str(n) if n is not None else s


def _replace_ymd(m: re.Match) -> str:
    y = _norm_year(m.group("year"))
    mo = _norm_md(m.group("month")).zfill(2)
    d = _norm_md(m.group("day")).zfill(2)
    return f"{y}年{mo}月{d}日"


def _replace_md(m: re.Match) -> str:
    mo = _norm_md(m.group("month"))
    d = _norm_md(m.group("day"))
    return f"{mo}月{d}日"


def _normalize_dates(text: str) -> str:
    text = _DATE_YMD_RE.sub(_replace_ymd, text)
    text = _DATE_MD_RE.sub(_replace_md, text)
    return text


# ═══════════════════════════════════════════════════════════════════════════════
# Times  下午三點十五分 → 下午3:15 / 上午十點半 → 上午10:30
# ═══════════════════════════════════════════════════════════════════════════════

# Hour/minute: same component as month/day
_HM_PAT = _MD_PAT

_TIME_RE = re.compile(
    rf"(?P<prefix>上午|下午|早上|晚上|凌晨)?"
    rf"(?P<hour>{_HM_PAT})點"
    rf"(?:(?P<half>半)|(?P<minute>{_HM_PAT})分|(?P<clock>鐘))?"
)


def _replace_time(m: re.Match) -> str:
    prefix = m.group("prefix") or ""
    hour = _cn_or_ascii_to_int(m.group("hour")) or 0

    if m.group("half"):
        return f"{prefix}{hour}:30"
    min_str = m.group("minute")
    if min_str:
        minute = _cn_or_ascii_to_int(min_str) or 0
        return f"{prefix}{hour}:{minute:02d}"
    # 鐘 or bare 點
    return f"{prefix}{hour}點"


def _normalize_times(text: str) -> str:
    return _TIME_RE.sub(_replace_time, text)


# ═══════════════════════════════════════════════════════════════════════════════
# General numbers  一千五百 → 1,500 / 兩萬三千 → 23,000
#
# Conservative pattern: requires at least one unit char (十百千萬億).
# Pure bare digits (一, 二) are left alone to avoid false positives.
# Exception: 十N (e.g. 十五) is also matched.
# ═══════════════════════════════════════════════════════════════════════════════

_GEN_NUM_RE = re.compile(
    rf"(?:"
    rf"[{_D}]+[{_U}][{_N}]*"   # digit(s) + unit + optional rest  (一千五百)
    rf"|"
    rf"十[{_D}]+"               # 十 + digit(s)  (十五, 十二)
    rf")"
)


def _replace_gen_num(m: re.Match) -> str:
    n = _parse_cn_num(m.group(0))
    if n is None:
        return m.group(0)
    return f"{n:,}"


def _normalize_numbers(text: str) -> str:
    return _GEN_NUM_RE.sub(_replace_gen_num, text)


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def normalize(text: str) -> str:
    """Apply all ITN rules to one segment of ASR text.

    Order matters: most-specific patterns run first to prevent partial matches
    by later, more general rules.
      1. Percentages (百分之N before 百 is eaten by number rule)
      2. Phones      (long digit runs before shorter number patterns)
      3. Dates       (年月日 before bare number pattern)
      4. Times       (N點 before bare number pattern)
      5. Numbers     (general 十百千萬億)
    """
    text = _normalize_percentages(text)
    text = _normalize_phones(text)
    text = _normalize_dates(text)
    text = _normalize_times(text)
    text = _normalize_numbers(text)
    return text


def apply_itn_to_segments(
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return segments with ITN applied to each segment's text field."""
    result = []
    for seg in segments:
        raw = seg.get("text") or ""
        normed = normalize(raw)
        result.append(seg if normed == raw else {**seg, "text": normed})
    return result
