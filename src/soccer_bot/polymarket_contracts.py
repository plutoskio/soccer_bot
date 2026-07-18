from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Mapping, Sequence

from .database import json_text, normalized_name, stable_id


class PolymarketContractError(RuntimeError):
    """Raised when a frozen Polymarket contract policy is invalid."""


@dataclass(frozen=True)
class ContractDecision:
    status: str
    contract_key: str | None
    period: str | None
    parameters: dict[str, object]
    rejection_reason: str | None
    outcomes: tuple[tuple[str, str, int], ...] = ()


def canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_polymarket_contract_policy(path: Path) -> tuple[dict, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise PolymarketContractError("Polymarket contract policy must be an object")
    required_strings = (
        "policy_version",
        "mapping_version",
        "provider",
        "access_mode",
        "period",
    )
    for key in required_strings:
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise PolymarketContractError(f"Invalid Polymarket policy field: {key}")
    if raw["provider"] != "polymarket":
        raise PolymarketContractError("Polymarket policy provider mismatch")
    if raw["access_mode"] != "public_read_only_market_data":
        raise PolymarketContractError("Polymarket policy must remain read-only")
    supported = raw.get("supported_market_types")
    phrases = raw.get("accepted_period_phrases")
    if not isinstance(supported, dict) or not supported:
        raise PolymarketContractError("supported_market_types must be non-empty")
    if not isinstance(phrases, dict) or not isinstance(phrases.get("default"), list):
        raise PolymarketContractError("accepted_period_phrases is invalid")
    guardrails = raw.get("guardrails")
    mandatory_guardrails = (
        "market_data_is_never_a_champion_model_feature",
        "first_valid_evidence_is_immutable",
        "no_result_or_settlement_fields",
        "no_orders_wallets_signatures_positions_or_credentials",
        "coverage_reporting_is_count_only",
        "ambiguous_contracts_fail_closed",
    )
    if not isinstance(guardrails, dict) or any(
        guardrails.get(key) is not True for key in mandatory_guardrails
    ):
        raise PolymarketContractError("Mandatory Polymarket guardrail is disabled")
    execution = raw.get("execution")
    if not isinstance(execution, dict) or execution.get("direction") != (
        "buy_selected_yes_token_as_taker"
    ):
        raise PolymarketContractError("Unsupported execution direction")
    quantities = execution.get("share_quantities")
    if (
        not isinstance(quantities, list)
        or not quantities
        or any(not _positive_finite(value) for value in quantities)
        or len({float(value) for value in quantities}) != len(quantities)
    ):
        raise PolymarketContractError("share_quantities must be unique positive values")
    return raw, canonical_json_sha256(raw)


def refresh_polymarket_contract_mappings(
    connection,
    *,
    policy: Mapping[str, object],
    policy_sha256: str,
    mapped_at: datetime | None = None,
) -> dict[str, int]:
    """Materialize one immutable, fail-closed semantic decision per market.

    Existing decisions under the same mapping version are never updated. A
    provider wording change therefore requires an explicit new policy version,
    preserving the exact interpretation used by prior evidence.
    """

    mapped_at = (mapped_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    mapping_version = str(policy["mapping_version"])
    rows = connection.execute(
        """
        SELECT m.prediction_market_id,e.fixture_id,m.market_type,m.question,
               m.line_value,m.rules_text,home.name,away.name,
               list(struct_pack(outcome_id := o.outcome_id,
                                outcome_name := o.outcome_name)
                    ORDER BY o.outcome_id)
        FROM prediction_market m
        JOIN prediction_market_event e USING (prediction_market_event_id)
        JOIN fixture f ON f.fixture_id=e.fixture_id
        JOIN team home ON home.team_id=f.home_team_id
        JOIN team away ON away.team_id=f.away_team_id
        JOIN prediction_market_outcome o USING (prediction_market_id)
        LEFT JOIN polymarket_contract_mapping existing
          ON existing.prediction_market_id=m.prediction_market_id
         AND existing.mapping_version=?
        WHERE e.fixture_id IS NOT NULL AND existing.mapping_id IS NULL
        GROUP BY m.prediction_market_id,e.fixture_id,m.market_type,m.question,
                 m.line_value,m.rules_text,home.name,away.name
        ORDER BY m.prediction_market_id
        """,
        [mapping_version],
    ).fetchall()
    counts = {"reviewed": 0, "accepted": 0, "rejected": 0}
    for (
        market_id,
        fixture_id,
        market_type,
        question,
        line_value,
        rules_text,
        home_name,
        away_name,
        outcome_structs,
    ) in rows:
        outcomes = tuple(
            (str(item["outcome_id"]), str(item["outcome_name"]))
            for item in outcome_structs
        )
        decision = classify_polymarket_contract(
            policy,
            market_type=str(market_type or ""),
            question=str(question or ""),
            line_value=line_value,
            rules_text=str(rules_text or ""),
            home_name=str(home_name),
            away_name=str(away_name),
            outcomes=outcomes,
        )
        mapping_id = stable_id("polymarket_contract_mapping", market_id, mapping_version)
        rules_hash = hashlib.sha256(str(rules_text or "").encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO polymarket_contract_mapping (
                mapping_id,prediction_market_id,fixture_id,mapping_version,
                mapping_policy_sha256,provider_market_type,contract_key,period,
                parameters,mapping_status,rejection_reason,rules_sha256,mapped_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (prediction_market_id,mapping_version) DO NOTHING
            """,
            [
                mapping_id,
                market_id,
                fixture_id,
                mapping_version,
                policy_sha256,
                str(market_type or ""),
                decision.contract_key,
                decision.period,
                json_text(decision.parameters),
                decision.status,
                decision.rejection_reason,
                rules_hash,
                mapped_at,
            ],
        )
        for outcome_id, selection, polarity in decision.outcomes:
            connection.execute(
                """
                INSERT INTO polymarket_contract_outcome_mapping
                    (mapping_id,outcome_id,canonical_selection,polarity)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (mapping_id,outcome_id) DO NOTHING
                """,
                [mapping_id, outcome_id, selection, polarity],
            )
        counts["reviewed"] += 1
        counts[decision.status] += 1
    return counts


def classify_polymarket_contract(
    policy: Mapping[str, object],
    *,
    market_type: str,
    question: str,
    line_value: object,
    rules_text: str,
    home_name: str,
    away_name: str,
    outcomes: Sequence[tuple[str, str]],
) -> ContractDecision:
    supported = policy["supported_market_types"]
    if market_type not in supported:
        return _reject("unsupported_provider_market_type")
    if not _period_is_regulation(policy, market_type, rules_text):
        return _reject("regulation_period_language_missing")
    outcome_by_name = {name: outcome_id for outcome_id, name in outcomes}
    if len(outcome_by_name) != len(outcomes):
        return _reject("duplicate_outcome_names")
    try:
        if market_type == "moneyline":
            try:
                selection = _moneyline_selection(question, home_name, away_name)
            except ValueError:
                return _reject("moneyline_question_or_team_ambiguous")
            return _binary_decision(
                str(supported[market_type]), selection, {}, outcome_by_name
            )
        if market_type == "totals":
            line = _validated_line(line_value)
            match = re.fullmatch(
                r"(.+) vs\. (.+): O/U ([+-]?\d+(?:\.\d+)?)",
                question.strip(),
            )
            if (
                not match
                or not _fixture_pair_matches(
                    match.group(1), match.group(2), home_name, away_name
                )
                or not math.isclose(float(match.group(3)), line, abs_tol=1e-9)
            ):
                return _reject("total_line_mismatch")
            return _two_way_decision(
                str(supported[market_type]),
                {"line": line},
                outcome_by_name,
                {"Over": "over", "Under": "under"},
            )
        if market_type == "spreads":
            line = _validated_line(line_value)
            match = re.fullmatch(
                r"Spread: (.+) \(([+-]?\d+(?:\.\d+)?)\)", question.strip()
            )
            if not match or not math.isclose(float(match.group(2)), line, abs_tol=1e-9):
                return _reject("spread_line_mismatch")
            named_side = _team_side(match.group(1), home_name, away_name)
            if named_side is None:
                return _reject("spread_named_team_ambiguous")
            home_handicap = line if named_side == "home" else -line
            mapped = _team_outcome_names(outcome_by_name, home_name, away_name)
            if mapped is None:
                return _reject("spread_outcomes_do_not_match_fixture_teams")
            return _accepted(
                str(supported[market_type]),
                {"home_handicap": home_handicap},
                tuple(
                    (outcome_id, f"{side}_cover", 1)
                    for side, outcome_id in mapped.items()
                ),
            )
        if market_type == "soccer_team_totals":
            line = _validated_line(line_value)
            match = re.fullmatch(
                r"(.+) vs\. (.+): (.+) O/U ([+-]?\d+(?:\.\d+)?)",
                question.strip(),
            )
            if (
                not match
                or not _fixture_pair_matches(
                    match.group(1), match.group(2), home_name, away_name
                )
                or not math.isclose(float(match.group(4)), line, abs_tol=1e-9)
            ):
                return _reject("team_total_line_mismatch")
            team_side = _team_side(match.group(3), home_name, away_name)
            if team_side is None:
                return _reject("team_total_team_ambiguous")
            return _two_way_decision(
                str(supported[market_type]),
                {"team": team_side, "line": line},
                outcome_by_name,
                {"Over": "over", "Under": "under"},
            )
        if market_type == "both_teams_to_score":
            match = re.fullmatch(
                r"(.+) vs\. (.+): Both Teams to Score", question.strip()
            )
            if not match or not _fixture_pair_matches(
                match.group(1), match.group(2), home_name, away_name
            ):
                return _reject("btts_question_shape_invalid")
            return _binary_decision(
                str(supported[market_type]), "yes", {}, outcome_by_name
            )
        if market_type == "soccer_exact_score":
            if question.strip() == "Exact Score: Any Other Score?":
                return _binary_decision(
                    str(supported[market_type]),
                    "other_score",
                    {"score_bucket": "other"},
                    outcome_by_name,
                )
            parsed_score = _exact_score(question, home_name, away_name)
            if parsed_score is None:
                return _reject("exact_score_question_ambiguous")
            home_goals, away_goals = parsed_score
            return _binary_decision(
                str(supported[market_type]),
                f"score_{home_goals}_{away_goals}",
                {"home_goals": home_goals, "away_goals": away_goals},
                outcome_by_name,
            )
    except (TypeError, ValueError):
        return _reject("invalid_numeric_parameter")
    return _reject("unhandled_supported_market_type")


def _period_is_regulation(
    policy: Mapping[str, object], market_type: str, rules_text: str
) -> bool:
    phrases = policy["accepted_period_phrases"]
    accepted = phrases.get(market_type, phrases["default"])
    normalized_rules = " ".join(rules_text.casefold().split())
    return bool(accepted) and any(
        " ".join(str(phrase).casefold().split()) in normalized_rules
        for phrase in accepted
    )


def _moneyline_selection(question: str, home_name: str, away_name: str) -> str:
    draw = re.fullmatch(
        r"Will (.+) vs\. (.+) end in a draw\?", question.strip()
    )
    if draw and _fixture_pair_matches(
        draw.group(1), draw.group(2), home_name, away_name
    ):
        return "draw"
    match = re.fullmatch(r"Will (.+) win on \d{4}-\d{2}-\d{2}\?", question.strip())
    if not match:
        raise ValueError("moneyline question")
    side = _team_side(match.group(1), home_name, away_name)
    if side is None:
        raise ValueError("moneyline team")
    return f"{side}_win"


def _binary_decision(
    contract_key: str,
    selection: str,
    parameters: dict[str, object],
    outcome_by_name: Mapping[str, str],
) -> ContractDecision:
    if set(outcome_by_name) != {"Yes", "No"}:
        return _reject("binary_outcomes_must_be_yes_no")
    return _accepted(
        contract_key,
        parameters,
        (
            (outcome_by_name["Yes"], selection, 1),
            (outcome_by_name["No"], selection, -1),
        ),
    )


def _two_way_decision(
    contract_key: str,
    parameters: dict[str, object],
    outcome_by_name: Mapping[str, str],
    expected: Mapping[str, str],
) -> ContractDecision:
    if set(outcome_by_name) != set(expected):
        return _reject("two_way_outcomes_mismatch")
    return _accepted(
        contract_key,
        parameters,
        tuple((outcome_by_name[name], selection, 1) for name, selection in expected.items()),
    )


def _accepted(
    contract_key: str,
    parameters: dict[str, object],
    outcomes: tuple[tuple[str, str, int], ...],
) -> ContractDecision:
    return ContractDecision(
        "accepted",
        contract_key,
        "regulation_plus_stoppage_time",
        parameters,
        None,
        outcomes,
    )


def _reject(reason: str) -> ContractDecision:
    return ContractDecision("rejected", None, None, {}, reason)


def _validated_line(value: object) -> float:
    line = float(value)
    if not math.isfinite(line):
        raise ValueError("line")
    return line


def _team_variants(name: str) -> set[str]:
    canonical = normalized_name(name)
    removable = {"fc", "cf", "afc", "sc", "club", "fk", "pfk", "bk"}
    variants = {canonical}
    words = canonical.split()
    while words and words[0] in removable:
        words.pop(0)
    while words and words[-1] in removable:
        words.pop()
    if words:
        variants.add(" ".join(words))
    return {value for value in variants if value}


def _team_side(value: str, home_name: str, away_name: str) -> str | None:
    candidate_variants = _team_variants(value)
    home = bool(candidate_variants.intersection(_team_variants(home_name)))
    away = bool(candidate_variants.intersection(_team_variants(away_name)))
    if home == away:
        return None
    return "home" if home else "away"


def _team_outcome_names(
    outcome_by_name: Mapping[str, str], home_name: str, away_name: str
) -> dict[str, str] | None:
    mapped: dict[str, str] = {}
    for name, outcome_id in outcome_by_name.items():
        side = _team_side(name, home_name, away_name)
        if side is None or side in mapped:
            return None
        mapped[side] = outcome_id
    return mapped if set(mapped) == {"home", "away"} else None


def _fixture_pair_matches(
    question_home: str,
    question_away: str,
    home_name: str,
    away_name: str,
) -> bool:
    return (
        _team_side(question_home, home_name, away_name) == "home"
        and _team_side(question_away, home_name, away_name) == "away"
    )


def _exact_score(
    question: str, home_name: str, away_name: str
) -> tuple[int, int] | None:
    prefix = "Exact Score: "
    if not question.startswith(prefix) or not question.endswith("?"):
        return None
    body = question[len(prefix) : -1]
    candidates: list[tuple[int, int]] = []
    for match in re.finditer(r" (\d+) - (\d+) ", body):
        left = body[: match.start()]
        right = body[match.end() :]
        if _team_side(left, home_name, away_name) == "home" and _team_side(
            right, home_name, away_name
        ) == "away":
            candidates.append((int(match.group(1)), int(match.group(2))))
    return candidates[0] if len(candidates) == 1 else None


def _positive_finite(value: object) -> bool:
    if isinstance(value, bool):
        return False
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0
