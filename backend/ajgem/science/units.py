"""Mass-fraction unit registry and exact conversions (FRD §6 "Units analytes and qualified values").

All arithmetic uses ``decimal.Decimal``. Conversions are only applied between
units of the same dimension; a basis mismatch (dry vs as-received) is never
silently converted.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

UNIT_REGISTRY_VERSION = "units-2026.11.0"

MASS_FRACTION = "mass_fraction"


@dataclass(frozen=True)
class UnitDef:
    code: str
    dimension: str
    # Multiplier that converts a value in this unit to ppm (mass fraction).
    to_ppm: Decimal


_UNITS: dict[str, UnitDef] = {
    "%": UnitDef("%", MASS_FRACTION, Decimal(10000)),
    "ppm": UnitDef("ppm", MASS_FRACTION, Decimal(1)),
    "ppb": UnitDef("ppb", MASS_FRACTION, Decimal("0.001")),
    "g/t": UnitDef("g/t", MASS_FRACTION, Decimal(1)),
    "mg/kg": UnitDef("mg/kg", MASS_FRACTION, Decimal(1)),
}

_ALIASES: dict[str, str] = {
    "%": "%",
    "pct": "%",
    "wt%": "%",
    "wt.%": "%",
    "percent": "%",
    "ppm": "ppm",
    "ppb": "ppb",
    "g/t": "g/t",
    "gpt": "g/t",
    "g/tonne": "g/t",
    "mg/kg": "mg/kg",
}

# Strings that look like units but have more than one plausible meaning.
_AMBIGUOUS: dict[str, str] = {
    "ppt": "parts per thousand or parts per trillion",
    "oz/t": "troy or avoirdupois ounce per short or metric ton",
    "opt": "troy ounce per short ton or other convention",
}


class UnitError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalise_unit(raw: str | None) -> str:
    """Return the canonical unit code or raise UnitError(UNIT_UNKNOWN|UNIT_AMBIGUOUS)."""
    if raw is None or not raw.strip():
        raise UnitError("UNIT_MISSING", "No unit supplied")
    key = raw.strip().lower().replace(" ", "")
    if key in _AMBIGUOUS:
        raise UnitError("UNIT_AMBIGUOUS", f"Unit '{raw}' is ambiguous: {_AMBIGUOUS[key]}")
    if key not in _ALIASES:
        raise UnitError("UNIT_UNKNOWN", f"Unit '{raw}' is not in registry {UNIT_REGISTRY_VERSION}")
    return _ALIASES[key]


def unit_def(code: str) -> UnitDef:
    return _UNITS[normalise_unit(code)]


def conversion_factor(from_unit: str, to_unit: str) -> Decimal:
    """Exact factor f such that value_in_to = value_in_from * f."""
    a, b = unit_def(from_unit), unit_def(to_unit)
    if a.dimension != b.dimension:
        raise UnitError("UNIT_INCOMPATIBLE", f"Cannot convert {a.code} to {b.code}")
    return a.to_ppm / b.to_ppm


def convert(value: Decimal, from_unit: str, to_unit: str) -> Decimal:
    return value * conversion_factor(from_unit, to_unit)


BASES = {"dry", "as_received", "unspecified"}


def normalise_basis(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        return "unspecified"
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"dry": "dry", "as_received": "as_received", "ar": "as_received", "wet": "as_received"}
    if key not in aliases:
        raise UnitError("BASIS_UNKNOWN", f"Measurement basis '{raw}' is not recognised")
    return aliases[key]
