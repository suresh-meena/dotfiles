from __future__ import annotations


DATA_CLASSES = {"PUBLIC", "INTERNAL", "CONFIDENTIAL", "SECRET", "UNKNOWN"}
ORDER = {"PUBLIC": 0, "INTERNAL": 1, "CONFIDENTIAL": 2, "SECRET": 3, "UNKNOWN": 999}


def allows_cloud(data_class: str, max_allowed: str) -> bool:
    # SECRET never routes to cloud per spec
    if data_class == "SECRET":
        return False
    if data_class == "UNKNOWN":
        return False
    # CONFIDENTIAL is local-only by default
    if data_class == "CONFIDENTIAL" and max_allowed not in ("CONFIDENTIAL", "SECRET"):
        # by default local-only; require explicit CONFIDENTIAL allowance
        return False
    return ORDER.get(data_class, 999) <= ORDER.get(max_allowed, -1)


def classify_path(path: str) -> str:
    low = path.lower()
    if low in (".env", ".env.local") or low.endswith(".pem") or low.endswith(".key") or ".secret" in low:
        return "SECRET"
    if any(s in low for s in ("token", "credential", "password")):
        return "SECRET"
    return "INTERNAL"
