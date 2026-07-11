from __future__ import annotations

from dataclasses import dataclass

from .player_names import (
    api_player_comparison_name,
    compatible_api_player_compound_names,
    compatible_api_player_names,
)


def deduplicate_api_lineup_entries(
    team_record: dict,
) -> tuple[list[tuple[str, dict]], list[dict], bool]:
    """Return unique lineup entries, preferring starter over substitute.

    API-Football occasionally repeats a starter in the substitutes array.  A
    duplicate inside the starting XI remains unrecoverable because that does
    not represent eleven distinct starters.
    """
    entries: list[tuple[str, dict]] = []
    seen: dict[tuple[str, str], str] = {}
    anomalies: list[dict] = []
    unrecoverable = False
    for role, field in (("starter", "startXI"), ("substitute", "substitutes")):
        for entry in team_record.get(field) or []:
            player = entry.get("player") or {}
            source_id = player.get("id")
            key = (
                ("id", str(source_id)) if source_id is not None
                else ("name", api_player_comparison_name(player.get("name", "Unknown")))
            )
            previous_role = seen.get(key)
            if previous_role is not None:
                anomalies.append({
                    "team_id": (team_record.get("team") or {}).get("id"),
                    "source_player_id": source_id,
                    "player_name": player.get("name", "Unknown"),
                    "first_role": previous_role,
                    "duplicate_role": role,
                })
                if previous_role == "starter" and role == "starter":
                    unrecoverable = True
                continue
            seen[key] = role
            entries.append((role, entry))
    return entries, anomalies, unrecoverable


MINIMUM_LINK_SCORE = 120
MINIMUM_SCORE_MARGIN = 25


@dataclass(frozen=True)
class LineupAlias:
    index: int
    source_player_id: str
    name: str
    role: str
    shirt_number: int | None
    position: str | None


@dataclass(frozen=True)
class StatCandidate:
    player_id: str
    source_player_ids: tuple[str, ...]
    name: str
    minutes_played: int | None
    started: bool | None
    shirt_number: int | None
    position: str | None
    recent_same_team: bool = False


@dataclass(frozen=True)
class CandidateScore:
    alias_index: int
    player_id: str
    score: int
    evidence: tuple[str, ...]
    rejected_reason: str | None = None


@dataclass(frozen=True)
class LinkDecision:
    alias_index: int
    player_id: str | None
    score: int
    confidence: float
    method: str
    evidence: tuple[str, ...]
    best_candidate_id: str | None = None
    unresolved_reason: str = "resolved"


def _numeric_source_id(value: str) -> str:
    return str(value).split("|", 1)[0]


def score_candidate(alias: LineupAlias, candidate: StatCandidate) -> CandidateScore:
    evidence: list[str] = []
    score = 0

    shirt_known = alias.shirt_number is not None and candidate.shirt_number is not None
    if shirt_known and int(alias.shirt_number) != int(candidate.shirt_number):
        # A current-match stat block is authoritative for the shirt number.
        # Historical same-team shirt numbers can change between seasons and
        # therefore provide positive evidence only when they match.
        if not candidate.recent_same_team:
            return CandidateScore(
                alias.index, candidate.player_id, -1000, (), "shirt_number_conflict"
            )
        shirt_known = False
    if shirt_known:
        score += 50
        evidence.append("shirt_number")

    expected_started = alias.role == "starter"
    if candidate.started is not None and bool(candidate.started) != expected_started:
        return CandidateScore(
            alias.index, candidate.player_id, -1000, tuple(evidence),
            "starter_status_conflict",
        )
    if candidate.started is not None:
        score += 30
        evidence.append("starter_status")

    alias_name = api_player_comparison_name(alias.name)
    candidate_name = api_player_comparison_name(candidate.name)
    name_evidence: str | None = None
    if alias_name and alias_name == candidate_name:
        score += 100
        evidence.append("exact_name")
        name_evidence = "exact_name"
    elif compatible_api_player_names(alias.name, candidate.name):
        score += 70
        evidence.append("abbreviated_name")
        name_evidence = "abbreviated_name"
    elif compatible_api_player_compound_names(alias.name, candidate.name):
        score += 50
        evidence.append("compound_name")
        name_evidence = "compound_name"

    alias_source_id = str(alias.source_player_id)
    provider_id_match = alias_source_id not in {"", "None"} and any(
        _numeric_source_id(value) == alias_source_id
        for value in candidate.source_player_ids
    )
    if provider_id_match and name_evidence is None:
        return CandidateScore(
            alias.index, candidate.player_id, -1000, tuple(evidence),
            "provider_id_name_conflict",
        )
    if provider_id_match:
        score += 60
        evidence.append("provider_id")

    if candidate.recent_same_team and name_evidence in {
        "exact_name", "abbreviated_name"
    }:
        score += 55
        evidence.append("recent_same_team")

    if candidate.minutes_played is not None and int(candidate.minutes_played) > 0:
        score += 10
        evidence.append("participated")

    return CandidateScore(alias.index, candidate.player_id, score, tuple(evidence))


def _qualified_edges(
    aliases: list[LineupAlias], candidates: list[StatCandidate]
) -> list[CandidateScore]:
    return [
        edge
        for alias in aliases
        for candidate in candidates
        if (edge := score_candidate(alias, candidate)).rejected_reason is None
        and edge.score >= MINIMUM_LINK_SCORE
    ]


def _unique_best(
    edges: list[CandidateScore], *, key: str
) -> dict[int | str, CandidateScore]:
    grouped: dict[int | str, list[CandidateScore]] = {}
    for edge in edges:
        value: int | str = edge.alias_index if key == "alias" else edge.player_id
        grouped.setdefault(value, []).append(edge)
    result = {}
    for value, options in grouped.items():
        options.sort(key=lambda edge: (-edge.score, edge.alias_index, edge.player_id))
        margin = options[0].score - options[1].score if len(options) > 1 else options[0].score
        if margin >= MINIMUM_SCORE_MARGIN:
            result[value] = options[0]
    return result


def link_team_players(
    aliases: list[LineupAlias], candidates: list[StatCandidate]
) -> dict[int, LinkDecision]:
    """Resolve only mutual, clearly superior matches for an entire team."""
    remaining_aliases = {alias.index for alias in aliases}
    remaining_players = {candidate.player_id for candidate in candidates}
    edges = _qualified_edges(aliases, candidates)
    decisions: dict[int, LinkDecision] = {}

    while remaining_aliases and remaining_players:
        active = [
            edge for edge in edges
            if edge.alias_index in remaining_aliases and edge.player_id in remaining_players
        ]
        if not active:
            break
        alias_best = _unique_best(active, key="alias")
        player_best = _unique_best(active, key="player")
        pairs = [
            edge for alias_index, edge in alias_best.items()
            if player_best.get(edge.player_id) == edge
        ]
        if not pairs:
            break
        for edge in sorted(pairs, key=lambda value: value.alias_index):
            if edge.alias_index not in remaining_aliases or edge.player_id not in remaining_players:
                continue
            if "provider_id" in edge.evidence and "exact_name" in edge.evidence:
                method = "exact_provider_identity"
            elif "provider_id" in edge.evidence:
                method = "provider_id_compatible_name"
            elif "recent_same_team" in edge.evidence:
                method = "recent_same_team"
            else:
                method = next(
                    (
                        name for name in (
                            "exact_name", "abbreviated_name", "compound_name"
                        ) if name in edge.evidence
                    ),
                    "contextual_evidence",
                )
            decisions[edge.alias_index] = LinkDecision(
                alias_index=edge.alias_index,
                player_id=edge.player_id,
                score=edge.score,
                confidence=min(0.99, 0.5 + edge.score / 400),
                method=method,
                evidence=edge.evidence,
                best_candidate_id=edge.player_id,
                unresolved_reason="resolved",
            )
            remaining_aliases.remove(edge.alias_index)
            remaining_players.remove(edge.player_id)

    all_scores = [score_candidate(alias, candidate) for alias in aliases for candidate in candidates]
    for alias in aliases:
        if alias.index in decisions:
            continue
        options = sorted(
            (
                edge for edge in all_scores
                if edge.alias_index == alias.index and edge.rejected_reason is None
            ),
            key=lambda edge: (-edge.score, edge.player_id),
        )
        best = options[0] if options else None
        qualified = [edge for edge in options if edge.score >= MINIMUM_LINK_SCORE]
        rejected_reasons = {
            edge.rejected_reason for edge in all_scores
            if edge.alias_index == alias.index and edge.rejected_reason
        }
        if "provider_id_name_conflict" in rejected_reasons:
            unresolved_reason = "provider_id_name_conflict"
        elif qualified:
            unresolved_reason = "ambiguous_candidates"
        elif best:
            unresolved_reason = "below_threshold"
        elif rejected_reasons:
            unresolved_reason = sorted(rejected_reasons)[0]
        else:
            unresolved_reason = "no_candidate"
        decisions[alias.index] = LinkDecision(
            alias_index=alias.index,
            player_id=None,
            score=best.score if best else 0,
            confidence=0.0,
            method="unresolved_alias",
            evidence=best.evidence if best else (),
            best_candidate_id=best.player_id if best else None,
            unresolved_reason=unresolved_reason,
        )
    return decisions


def can_auto_reconcile(decision: LinkDecision) -> bool:
    """Return whether a post-match alias can be relinked without review.

    Provider identity plus a compatible name is the strongest evidence.  An
    exact name can also be accepted when the same fixture supplies lineup
    context (shirt or starter status).  Historical same-team evidence alone
    remains a review candidate and never silently rewrites a lineup link.
    """
    if decision.player_id is None or "recent_same_team" in decision.evidence:
        return False
    if "provider_id" in decision.evidence:
        return True
    return "exact_name" in decision.evidence and bool(
        {"shirt_number", "starter_status"} & set(decision.evidence)
    )
