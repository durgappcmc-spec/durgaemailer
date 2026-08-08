# NOTE: Connector instances are cached in-process; restart Streamlit to pick up new keys.
from __future__ import annotations

import sys
from typing import Any, Optional

from connectors.apollo import ApolloConnector
from connectors.rocketreach import RocketReachConnector
from connectors.zoominfo import ZoomInfoConnector

_registry: dict[str, Any] = {}

_FACTORIES = {
    "apollo": ApolloConnector,
    "zoominfo": ZoomInfoConnector,
    "rocketreach": RocketReachConnector,
}


def get_connector(name: str):
    """Lazy-instantiate and cache a connector by name."""
    key = name.lower().strip()
    if key not in _FACTORIES:
        raise ValueError(f"Unknown provider: {name}")
    if key not in _registry:
        _registry[key] = _FACTORIES[key]()
    return _registry[key]


def _dedupe_key(p: dict[str, Any]) -> str:
    email = (p.get("email") or "").strip().lower()
    if email:
        return f"email:{email}"
    linkedin = (p.get("linkedin_url") or "").strip().lower().rstrip("/")
    if linkedin:
        return f"li:{linkedin}"
    name = (p.get("name") or "").strip().lower()
    company = (p.get("company") or "").strip().lower()
    return f"nc:{name}|{company}"


def search_all(
    query: dict[str, Any],
    providers: tuple[str, ...] | list[str] = ("apollo", "zoominfo", "rocketreach"),
    limit_per_provider: int = 10,
) -> list[dict[str, Any]]:
    """Fan out search across providers and dedupe results."""
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in providers:
        try:
            conn = get_connector(name)
            rows = conn.search(query, limit=limit_per_provider)
        except Exception as e:
            print(f"[{name}] search_all error: {e}", file=sys.stderr)
            results.append({"source": name, "error": str(e)})
            continue
        for row in rows:
            if "error" in row:
                results.append(row)
                continue
            key = _dedupe_key(row)
            if key in seen and key != "nc:|":
                continue
            seen.add(key)
            results.append(row)
    return results


def enrich_fallthrough(
    identifier: dict[str, Any],
    order: tuple[str, ...] | list[str] = ("rocketreach", "apollo", "zoominfo"),
) -> dict[str, Any]:
    """Try providers in order; return first result that has an email."""
    errors: list[dict[str, str]] = []
    last_without_email: Optional[dict[str, Any]] = None
    for name in order:
        try:
            conn = get_connector(name)
            result = conn.enrich(identifier)
            if result and result.get("email"):
                return result
            if result:
                last_without_email = result
                errors.append({"source": name, "error": "no email in result"})
            else:
                errors.append({"source": name, "error": "no match"})
        except Exception as e:
            print(f"[{name}] enrich_fallthrough error: {e}", file=sys.stderr)
            errors.append({"source": name, "error": str(e)})
    if last_without_email:
        last_without_email["enrich_errors"] = errors
        return last_without_email
    return {"error": "no enrichment result", "errors": errors, "source": "fallthrough"}
