"""Versioned analyte registry and gated oxide-equivalent conversions.

The FRD requires conversion factors to be "versioned and independently approved
before activation". Factors therefore ship with ``approved_by=None`` and
:func:`oxide_equivalent` refuses to use an unapproved factor. Approval is an
explicit, recorded act (see ``AnalyteRegistry.approve``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

ANALYTE_REGISTRY_VERSION = "analytes-2026.11.0"


@dataclass(frozen=True)
class Analyte:
    code: str
    kind: str  # "element" | "oxide"
    element: str
    name: str


@dataclass(frozen=True)
class OxideFactor:
    element: str
    oxide: str
    factor: Decimal
    version: str
    provenance: str
    approved_by: str | None = None
    approved_at: datetime | None = None

    @property
    def approved(self) -> bool:
        return self.approved_by is not None


_ANALYTES = [
    Analyte("Li", "element", "Li", "Lithium"),
    Analyte("Li2O", "oxide", "Li", "Lithium oxide"),
    Analyte("Sn", "element", "Sn", "Tin"),
    Analyte("SnO2", "oxide", "Sn", "Tin dioxide"),
    Analyte("Ta", "element", "Ta", "Tantalum"),
    Analyte("Ta2O5", "oxide", "Ta", "Tantalum pentoxide"),
    Analyte("Nb", "element", "Nb", "Niobium"),
    Analyte("Nb2O5", "oxide", "Nb", "Niobium pentoxide"),
    Analyte("Au", "element", "Au", "Gold"),
    Analyte("Cs", "element", "Cs", "Caesium"),
    Analyte("Rb", "element", "Rb", "Rubidium"),
    Analyte("Be", "element", "Be", "Beryllium"),
]

# Only the Li factor is referenced by the FRD. Its value is the FRD's stated
# approximate factor; authoritative atomic-weight provenance is an open
# verification item (FRD §6), hence it ships unapproved.
_FACTORS = [
    OxideFactor(
        element="Li",
        oxide="Li2O",
        factor=Decimal("2.153"),
        version="Li-Li2O-2026.11-draft",
        provenance="FRD v1.0 §6 approximate stoichiometric factor; atomic-weight source pending verification",
    ),
]


class ConversionNotApproved(Exception):
    code = "CONVERSION_NOT_APPROVED"


class UnknownAnalyte(ValueError):
    code = "ANALYTE_UNKNOWN"


@dataclass
class AnalyteRegistry:
    analytes: dict[str, Analyte] = field(default_factory=lambda: {a.code.lower(): a for a in _ANALYTES})
    factors: dict[tuple[str, str], OxideFactor] = field(
        default_factory=lambda: {(f.element, f.oxide): f for f in _FACTORS}
    )
    version: str = ANALYTE_REGISTRY_VERSION

    def get(self, code: str) -> Analyte:
        key = (code or "").strip().lower()
        if key not in self.analytes:
            raise UnknownAnalyte(f"Analyte '{code}' is not in registry {self.version}")
        return self.analytes[key]

    def factor(self, element: str, oxide: str) -> OxideFactor:
        try:
            return self.factors[(element, oxide)]
        except KeyError:
            raise ConversionNotApproved(f"No registered factor {element}->{oxide}") from None

    def approve(self, element: str, oxide: str, approver: str) -> OxideFactor:
        f = replace(self.factor(element, oxide), approved_by=approver, approved_at=datetime.now(timezone.utc))
        self.factors[(element, oxide)] = f
        return f


def oxide_equivalent(
    registry: AnalyteRegistry, element_value: Decimal, element: str, oxide: str, places: int = 3
) -> dict:
    """Return the calculated oxide equivalent with its factor, version and rounding disclosed."""
    f = registry.factor(element, oxide)
    if not f.approved:
        raise ConversionNotApproved(f"Factor {f.version} ({element}->{oxide}) has not been approved for use")
    exact = element_value * f.factor
    quant = Decimal(1).scaleb(-places)
    return {
        "oxide": oxide,
        "exact": exact,
        "display": exact.quantize(quant, rounding=ROUND_HALF_EVEN),
        "factor": f.factor,
        "factor_version": f.version,
        "factor_approved_by": f.approved_by,
        "rounding": f"ROUND_HALF_EVEN to {places} dp",
        "note": "Calculated equivalent of reported composition; not a mineralogical measurement.",
    }
