"""Project-resolution by domain ranking — `resolve-domain` subcommand core.

Pure logic, zero LLM cost. Given the addresses on a message and the
operator's project_map, decide which project the message belongs to.

Algorithm:

1. Walk From / To / Cc, weighting each occurrence by role
   (From=3, To=2, Cc=1). Higher weight = stronger ownership signal.
2. Drop addresses whose domain matches user_domains (us, not them) or
   exclude_domains (vendor noise like DocuSign).
3. Group remaining addresses by domain; sum role weights and counts.
4. Rank domains: mapped (in project_map) above unmapped, then by score
   descending, then alphabetically by domain.
5. ``matched_project`` is the project name of the first mapped domain
   in the ranking — or ``None`` if no domain was mapped.

The output also includes a ``decision_trace`` of human-readable
strings so operators can audit why a project was (not) picked.
"""

from __future__ import annotations

from typing import Any

from lib.headers import domain_of

ROLE_WEIGHTS = {"from": 3, "to": 2, "cc": 1}


def resolve(
    *,
    from_: list[dict[str, str]],
    to: list[dict[str, str]],
    cc: list[dict[str, str]],
    user_domains: list[str],
    exclude_domains: list[str],
    project_map: dict[str, str],
) -> dict[str, Any]:
    """See module docstring for the algorithm; returns a dict with three keys.

    ``matched_project`` — the project name we resolved to (or ``None``).
    ``ranked_domains`` — every relevant domain, sorted by the rules above.
    ``decision_trace`` — human-readable explanation strings.
    """
    user_set = {d.lower() for d in user_domains}
    exclude_set = {d.lower() for d in exclude_domains}
    project_map_lower = {k.lower(): v for k, v in project_map.items()}

    trace: list[str] = []

    # Phase 1: collect (domain, role) tuples from each address list.
    occurrences: list[tuple[str, str]] = []
    for role, addrs in (("from", from_), ("to", to), ("cc", cc)):
        for entry in addrs or []:
            d = domain_of(entry.get("addr"))
            if d:
                occurrences.append((d, role))

    if not occurrences:
        trace.append("no addresses with valid domains; cannot resolve")
        return {
            "matched_project": None,
            "ranked_domains": [],
            "decision_trace": trace,
        }

    # Phase 2: filter user / exclude domains.
    kept: list[tuple[str, str]] = []
    for d, role in occurrences:
        if d in user_set:
            continue
        if d in exclude_set:
            continue
        kept.append((d, role))

    if not kept:
        trace.append(
            "all addresses were user-domains-only or excluded — nothing left to rank"
        )
        return {
            "matched_project": None,
            "ranked_domains": [],
            "decision_trace": trace,
        }

    # Phase 3: aggregate per-domain.
    by_domain: dict[str, dict[str, Any]] = {}
    for d, role in kept:
        bucket = by_domain.setdefault(
            d,
            {
                "domain": d,
                "score": 0,
                "count": 0,
                "roles": [],
                "project": project_map_lower.get(d),
            },
        )
        bucket["score"] += ROLE_WEIGHTS[role]
        bucket["count"] += 1
        bucket["roles"].append(role)

    # Phase 4: rank — mapped domains first, then by score desc, then by
    # alphabetical domain ascending. Stable sort over a tuple key.
    def sort_key(b: dict[str, Any]) -> tuple[int, int, str]:
        is_unmapped = 1 if b["project"] is None else 0
        return (is_unmapped, -b["score"], b["domain"])

    ranked = sorted(by_domain.values(), key=sort_key)

    # Phase 5: pick the project.
    matched: str | None = None
    for entry in ranked:
        if entry["project"] is not None:
            matched = entry["project"]
            trace.append(
                f"top mapped domain={entry['domain']} score={entry['score']} "
                f"→ project={entry['project']!r}"
            )
            break
    if matched is None:
        trace.append(
            "no domains found in project_map; "
            f"ranked {len(ranked)} unmapped domain(s)"
        )

    return {
        "matched_project": matched,
        "ranked_domains": ranked,
        "decision_trace": trace,
    }
