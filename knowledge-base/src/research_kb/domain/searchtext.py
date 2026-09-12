from __future__ import annotations

import unicodedata
from typing import Any

GREEK_NAMES = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "Δ": "Delta",
    "ε": "epsilon",
    "θ": "theta",
    "λ": "lambda",
    "μ": "mu",
    "ν": "nu",
    "π": "pi",
    "ρ": "rho",
    "σ": "sigma",
    "τ": "tau",
    "φ": "phi",
    "χ": "chi",
    "ψ": "psi",
    "ω": "omega",
    "Ω": "Omega",
}


def _collect(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, bool) or value is None:
        return
    elif isinstance(value, (int, float)):
        out.append(str(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            out.append(str(key))
            _collect(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect(item, out)


def notation_variants(text: str) -> list[str]:
    variants: list[str] = []
    named = "".join(GREEK_NAMES.get(char, char) for char in text)
    if named != text:
        variants.append(named)
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    if stripped != text:
        variants.append(stripped)
    if "Delta " in named:
        variants.append(named.replace("Delta ", "d"))
        variants.append(named.replace("Delta ", "Δ"))
    return variants


def semantic_text(state: dict[str, Any], body_md: str = "", title: str = "") -> str:
    parts: list[str] = []
    _collect(state, parts)
    if title:
        parts.append(title)
    if body_md:
        parts.append(body_md)
    seen: set[str] = set()
    normalized: list[str] = []
    for part in parts:
        stripped = part.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        normalized.append(stripped)
        for variant in notation_variants(stripped):
            if variant and variant not in seen:
                seen.add(variant)
                normalized.append(variant)
    return "\n".join(normalized)
