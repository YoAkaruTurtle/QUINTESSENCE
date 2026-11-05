"""PCS request-budget units and the pure calculation shared by the runtime.

``U(text)`` is the one estimation unit used by every PCS-side budget value
(``INJECTION_BUDGET_UNITS`` and the host-configured ``request_budget_units``).
It is deliberately *not* a model token count: no tokenizer is imported here and
no provider capacity is implied.

The rules are fixed by the J03 card:

* an ASCII letter/digit, or one of the fixed ASCII whitespace characters
  ``U+0020`` and ``U+0009``-``U+000D``, costs one quarter unit;
* every other character costs four quarters;
* a character above ``U+007F`` whose Unicode major category is not one of
  ``L``, ``N``, ``M``, ``P`` or ``Z`` costs four more quarters per additional
  UTF-8 byte;
* ``U = max(1, ceil(quarters / 4))``.

``U+001C``-``U+001F`` are deliberately *not* whitespace here even though
``str.isspace()`` says otherwise: the fixed set above is the contract, and a
slightly larger estimate is the conservative choice.
"""

from __future__ import annotations

import unicodedata

#: The PCS cap on the content it injects into one model request, in ``U``.
INJECTION_BUDGET_UNITS = 4096

#: Absorbs estimation error, request-envelope overhead and formatting marks.
SAFETY_MARGIN_UNITS = 256

#: The fixed ASCII whitespace set (the contract, not ``str.isspace()``).
ASCII_WHITESPACE = frozenset(" \t\n\v\f\r")

_ASCII_ALNUM = frozenset(
    chr(code) for code in range(0x80) if chr(code).isalnum()
)


def quarter_units(text: str) -> int:
    """Return the quarter-unit cost of ``text`` before rounding to whole units."""

    if not isinstance(text, str):
        raise TypeError("text must be a str")
    quarters = 0
    for char in text:
        if char in _ASCII_ALNUM or char in ASCII_WHITESPACE:
            quarters += 1
            continue
        quarters += 4
        if ord(char) > 0x7F:
            if unicodedata.category(char)[0] not in "LNMPZ":
                quarters += 4 * (len(char.encode("utf-8")) - 1)
    return quarters


def estimate_units(text: str) -> int:
    """Return ``U(text)``: whole units, rounded up, with a floor of one."""

    return max(1, (quarter_units(text) + 3) // 4)


def request_units(body_units: int, output_reserve_units: int) -> int:
    """The whole-request cost: body plus output reserve plus the safety margin."""

    return body_units + output_reserve_units + SAFETY_MARGIN_UNITS


def available_injection_units(
    request_budget_units: int,
    *,
    injection_budget_units: int = INJECTION_BUDGET_UNITS,
    output_reserve_units: int = 0,
) -> int:
    """How much injected content fits under both PCS caps, in ``U``.

    The result may be negative: a request budget smaller than the reserve and
    margin simply cannot fit anything, and the caller must refuse to send.
    """

    rest = output_reserve_units + SAFETY_MARGIN_UNITS
    return min(injection_budget_units, request_budget_units - rest)


def fits_request(
    body_units: int,
    request_budget_units: int,
    *,
    output_reserve_units: int = 0,
) -> bool:
    """Whether a body of ``body_units`` fits the whole-request budget."""

    return request_units(body_units, output_reserve_units) <= request_budget_units


__all__ = [
    "ASCII_WHITESPACE",
    "INJECTION_BUDGET_UNITS",
    "SAFETY_MARGIN_UNITS",
    "available_injection_units",
    "estimate_units",
    "fits_request",
    "quarter_units",
    "request_units",
]
