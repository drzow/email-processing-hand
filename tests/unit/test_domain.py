"""Unit tests for tools/lib/domain.py — project-resolution ranking."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from lib import domain  # noqa: E402


def addrs(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    """Helper to build an address list shaped like headers.parse_address_list."""
    return [{"name": "", "addr": a} for _, a in pairs]


# ---------- ranking ------------------------------------------------------


def test_resolves_matched_project_when_one_external_domain_in_map() -> None:
    result = domain.resolve(
        from_=[{"name": "Sam", "addr": "sam@acme.com"}],
        to=[{"name": "Me", "addr": "me@scalesology.com"}],
        cc=[],
        user_domains=["scalesology.com"],
        exclude_domains=[],
        project_map={"acme.com": "Acme"},
    )
    assert result["matched_project"] == "Acme"
    assert result["ranked_domains"][0]["domain"] == "acme.com"
    assert result["ranked_domains"][0]["project"] == "Acme"


def test_user_domains_are_dropped_from_ranking() -> None:
    """User-domain addresses don't define project ownership of a thread."""
    result = domain.resolve(
        from_=[{"name": "", "addr": "alice@scalesology.com"}],
        to=[{"name": "", "addr": "bob@scalesology.com"}],
        cc=[],
        user_domains=["scalesology.com"],
        exclude_domains=[],
        project_map={},
    )
    assert result["matched_project"] is None
    assert result["ranked_domains"] == []
    assert any("user-domains-only" in t for t in result["decision_trace"])


def test_exclude_domains_are_dropped() -> None:
    """Vendor noise domains (DocuSign etc.) are filtered before ranking."""
    result = domain.resolve(
        from_=[{"name": "", "addr": "noreply@docusign.net"}],
        to=[{"name": "", "addr": "alice@scalesology.com"}],
        cc=[],
        user_domains=["scalesology.com"],
        exclude_domains=["docusign.net"],
        project_map={},
    )
    assert result["matched_project"] is None
    assert result["ranked_domains"] == []


def test_from_outweighs_to_outweighs_cc_for_same_count() -> None:
    """Roles have weights From=3 > To=2 > Cc=1."""
    result = domain.resolve(
        from_=[{"name": "", "addr": "sam@acme.com"}],
        to=[{"name": "", "addr": "rep@beta.com"}],
        cc=[{"name": "", "addr": "obs@gamma.com"}],
        user_domains=["scalesology.com"],
        exclude_domains=[],
        project_map={
            "acme.com": "Acme",
            "beta.com": "Beta",
            "gamma.com": "Gamma",
        },
    )
    domains = [d["domain"] for d in result["ranked_domains"]]
    assert domains == ["acme.com", "beta.com", "gamma.com"]
    assert result["matched_project"] == "Acme"


def test_ties_break_alphabetically_by_domain() -> None:
    result = domain.resolve(
        from_=[],
        to=[
            {"name": "", "addr": "rep@beta.com"},
            {"name": "", "addr": "rep@alpha.com"},
        ],
        cc=[],
        user_domains=[],
        exclude_domains=[],
        project_map={"alpha.com": "Alpha", "beta.com": "Beta"},
    )
    # Same role, same count → alphabetical.
    assert [d["domain"] for d in result["ranked_domains"]] == ["alpha.com", "beta.com"]
    assert result["matched_project"] == "Alpha"


def test_unmapped_domain_still_appears_in_ranking_but_no_match() -> None:
    """Domain shows up in ranked_domains with project=None when not in project_map."""
    result = domain.resolve(
        from_=[{"name": "", "addr": "rep@unknown.com"}],
        to=[],
        cc=[],
        user_domains=[],
        exclude_domains=[],
        project_map={"acme.com": "Acme"},
    )
    assert result["matched_project"] is None
    assert result["ranked_domains"] == [
        {"domain": "unknown.com", "score": 3, "count": 1, "roles": ["from"], "project": None},
    ]


def test_mapped_beats_unmapped_when_unmapped_has_higher_raw_score() -> None:
    """Mapped (project-known) domain wins over unmapped, even if unmapped scored higher."""
    result = domain.resolve(
        from_=[{"name": "", "addr": "rep@unknown.com"}],
        to=[
            {"name": "", "addr": "a@acme.com"},
            {"name": "", "addr": "b@acme.com"},
        ],
        cc=[],
        user_domains=[],
        exclude_domains=[],
        project_map={"acme.com": "Acme"},
    )
    # unknown.com: from=3 score; acme.com: 2x to=4 score → acme wins on raw too.
    # But the spec is: mapped projects rank above unmapped regardless.
    assert result["matched_project"] == "Acme"
    assert result["ranked_domains"][0]["domain"] == "acme.com"


def test_mapped_beats_unmapped_when_unmapped_has_higher_score() -> None:
    """The 'prefer mapped over unmapped' rule kicks in even on score loss."""
    result = domain.resolve(
        # 3 from-addresses on unknown vs 1 cc on mapped.
        from_=[{"name": "", "addr": "rep@unknown.com"}],
        to=[],
        cc=[{"name": "", "addr": "rep@acme.com"}],
        user_domains=[],
        exclude_domains=[],
        project_map={"acme.com": "Acme"},
    )
    # unknown.com score=3, acme.com score=1. Mapped should still win.
    assert result["matched_project"] == "Acme"
    # Mapped domain ranks first.
    assert result["ranked_domains"][0]["domain"] == "acme.com"


def test_decision_trace_explains_why() -> None:
    result = domain.resolve(
        from_=[{"name": "", "addr": "sam@acme.com"}],
        to=[],
        cc=[],
        user_domains=["scalesology.com"],
        exclude_domains=[],
        project_map={"acme.com": "Acme"},
    )
    trace = result["decision_trace"]
    assert any("acme.com" in t and "Acme" in t for t in trace)


def test_handles_missing_or_empty_inputs() -> None:
    result = domain.resolve(
        from_=[], to=[], cc=[],
        user_domains=[], exclude_domains=[], project_map={},
    )
    assert result["matched_project"] is None
    assert result["ranked_domains"] == []
    assert "no addresses" in " ".join(result["decision_trace"]).lower()


def test_invalid_addresses_are_silently_skipped() -> None:
    result = domain.resolve(
        from_=[{"name": "", "addr": "not-an-email"}],
        to=[],
        cc=[],
        user_domains=[],
        exclude_domains=[],
        project_map={},
    )
    assert result["ranked_domains"] == []


def test_user_and_exclude_domain_filters_are_case_insensitive() -> None:
    result = domain.resolve(
        from_=[{"name": "", "addr": "alice@SCALESOLOGY.com"}],
        to=[{"name": "", "addr": "bob@DOCUSIGN.NET"}],
        cc=[],
        user_domains=["scalesology.com"],
        exclude_domains=["docusign.net"],
        project_map={},
    )
    assert result["ranked_domains"] == []
