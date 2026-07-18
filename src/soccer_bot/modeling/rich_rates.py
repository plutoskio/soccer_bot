from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path

from soccer_bot.config import load_json
from soccer_bot.datasets.features import (
    RegulationFeatureRow,
    RegulationInferenceFeatureRow,
)
from soccer_bot.modeling.walk_forward import (
    WalkForwardConfig,
    WalkForwardPrediction,
    block_bootstrap_interval,
    comparison_seed,
    moneyline_probabilities,
)


class RichRateResearchError(RuntimeError):
    """Raised when rich-rate research violates chronology or coverage policy."""


@dataclass(frozen=True)
class MetricConfig:
    source_code: str
    column: str
    prior_mean: float
    prior_strength: float
    half_life_days: float


@dataclass(frozen=True)
class RichRateConfig:
    feature_version: str
    result_availability_delay_minutes: int
    xg: MetricConfig
    shots: MetricConfig
    fit_end_exclusive: datetime
    validation_end_exclusive: datetime
    minimum_fit_fixtures: int
    full_signal_history_matches: int
    ridge_penalty: float
    maximum_newton_iterations: int
    optimizer_tolerance: float
    minimum_expected_goals: float
    maximum_expected_goals: float


@dataclass(frozen=True)
class FixturePerformance:
    fixture_id: str
    home_xg: float | None
    away_xg: float | None
    home_shots: float | None
    away_shots: float | None
    available_at: datetime | None = None
    source_max_retrieved_at: datetime | None = None


@dataclass(frozen=True)
class RichRateFeatureRow:
    feature_version: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    home_team_id: str
    away_team_id: str
    home_xg_attack: float
    home_xg_defense: float
    away_xg_attack: float
    away_xg_defense: float
    home_shots_attack: float
    home_shots_defense: float
    away_shots_attack: float
    away_shots_defense: float
    home_xg_history: int
    away_xg_history: int
    home_shots_history: int
    away_shots_history: int


@dataclass(frozen=True)
class RichRateInferenceFeatureRow(RichRateFeatureRow):
    source_max_retrieved_at: datetime | None = None


@dataclass(frozen=True)
class RichRateFit:
    information_state: str
    fit_fixtures: int
    fit_kickoff_end_exclusive: datetime
    coefficients: dict[str, float]
    converged: bool
    iterations: int


@dataclass(frozen=True)
class RichRatePrediction:
    model_key: str
    fixture_id: str
    information_state: str
    prediction_at: datetime
    kickoff: datetime
    fold_key: str
    result: str
    expected_home_goals: float
    expected_away_goals: float
    home_win_probability: float
    draw_probability: float
    away_win_probability: float
    moneyline_log_loss: float
    moneyline_brier: float
    base_moneyline_log_loss: float
    base_moneyline_brier: float


@dataclass
class _EvidenceState:
    prior_mean: float
    prior_strength: float
    evidence_sum: float = 0.0
    evidence_weight: float = 0.0
    last_time: datetime | None = None

    def project(self, at: datetime, half_life_days: float) -> None:
        if self.last_time is None:
            self.last_time = at
            return
        days = max(0.0, (at - self.last_time).total_seconds() / 86400.0)
        if days:
            decay = math.exp(-math.log(2.0) * days / half_life_days)
            self.evidence_sum *= decay
            self.evidence_weight *= decay
            self.last_time = at

    @property
    def mean(self) -> float:
        return (
            self.prior_strength * self.prior_mean + self.evidence_sum
        ) / (self.prior_strength + self.evidence_weight)

    def update(self, value: float) -> None:
        self.evidence_sum += value
        self.evidence_weight += 1.0


@dataclass
class _RichTeamState:
    xg_attack: _EvidenceState
    xg_defense: _EvidenceState
    shots_attack: _EvidenceState
    shots_defense: _EvidenceState
    xg_history: int = 0
    shots_history: int = 0
    source_max_retrieved_at: datetime | None = None


def load_rich_rate_config(path: Path) -> RichRateConfig:
    raw = load_json(path)
    metrics = raw.get("metrics", {})
    research = raw.get("research_policy", {})
    if research.get("test_fold_access") is not False:
        raise RichRateResearchError("Rich-rate research must not access the test fold")

    def metric(name: str) -> MetricConfig:
        value = metrics[name]
        return MetricConfig(
            source_code=str(value["source_code"]),
            column=str(value["column"]),
            prior_mean=float(value["prior_mean"]),
            prior_strength=float(value["prior_strength_matches"]),
            half_life_days=float(value["half_life_days"]),
        )

    config = RichRateConfig(
        feature_version=str(raw.get("feature_version", "")),
        result_availability_delay_minutes=int(
            raw["result_availability_delay_minutes"]
        ),
        xg=metric("xg"),
        shots=metric("shots"),
        fit_end_exclusive=datetime.fromisoformat(
            research["fit_kickoff_end_exclusive"]
        ),
        validation_end_exclusive=datetime.fromisoformat(
            research["validation_kickoff_end_exclusive"]
        ),
        minimum_fit_fixtures=int(
            research["minimum_fit_fixtures_per_horizon"]
        ),
        full_signal_history_matches=int(
            research["full_signal_history_matches"]
        ),
        ridge_penalty=float(research["ridge_penalty"]),
        maximum_newton_iterations=int(research["maximum_newton_iterations"]),
        optimizer_tolerance=float(research["optimizer_tolerance"]),
        minimum_expected_goals=float(research["minimum_expected_goals"]),
        maximum_expected_goals=float(research["maximum_expected_goals"]),
    )
    if not config.feature_version:
        raise RichRateResearchError("feature_version is required")
    if config.fit_end_exclusive >= config.validation_end_exclusive:
        raise RichRateResearchError("Research fit must precede validation")
    if min(
        config.xg.prior_mean,
        config.xg.prior_strength,
        config.xg.half_life_days,
        config.shots.prior_mean,
        config.shots.prior_strength,
        config.shots.half_life_days,
        config.minimum_fit_fixtures,
        config.full_signal_history_matches,
        config.ridge_penalty,
        config.maximum_newton_iterations,
        config.optimizer_tolerance,
        config.minimum_expected_goals,
    ) <= 0:
        raise RichRateResearchError("Rich-rate priors and bounds must be positive")
    return config


def load_fixture_performance(
    connection,
    config: RichRateConfig,
    *,
    strict_retrieval_from: datetime | None = None,
) -> dict[str, FixturePerformance]:
    if strict_retrieval_from is not None and strict_retrieval_from.tzinfo is None:
        raise ValueError("strict_retrieval_from must be timezone-aware")
    rows = connection.execute(
        """
        SELECT
            f.fixture_id,
            max(CASE WHEN s.source_code=? AND s.team_id=f.home_team_id THEN s.xg END),
            max(CASE WHEN s.source_code=? AND s.team_id=f.away_team_id THEN s.xg END),
            max(CASE WHEN s.source_code=? AND s.team_id=f.home_team_id THEN s.shots END),
            max(CASE WHEN s.source_code=? AND s.team_id=f.away_team_id THEN s.shots END),
            count(*) FILTER (WHERE s.source_code=? AND s.team_id=f.home_team_id
                             AND s.xg IS NOT NULL),
            count(*) FILTER (WHERE s.source_code=? AND s.team_id=f.away_team_id
                             AND s.xg IS NOT NULL),
            count(*) FILTER (WHERE s.source_code=? AND s.team_id=f.home_team_id
                             AND s.shots IS NOT NULL),
            count(*) FILTER (WHERE s.source_code=? AND s.team_id=f.away_team_id
                             AND s.shots IS NOT NULL),
            f.scheduled_kickoff,
            max(s.retrieved_at) FILTER (
                WHERE s.source_code=? AND s.xg IS NOT NULL
                  AND s.team_id IN (f.home_team_id, f.away_team_id)
            ),
            max(s.retrieved_at) FILTER (
                WHERE s.source_code=? AND s.shots IS NOT NULL
                  AND s.team_id IN (f.home_team_id, f.away_team_id)
            )
        FROM fixture f
        JOIN team_match_stat_observation s USING (fixture_id)
        WHERE s.period='regulation' AND s.source_code IN (?, ?)
        GROUP BY f.fixture_id, f.scheduled_kickoff
        """,
        [
            config.xg.source_code,
            config.xg.source_code,
            config.shots.source_code,
            config.shots.source_code,
            config.xg.source_code,
            config.xg.source_code,
            config.shots.source_code,
            config.shots.source_code,
            config.xg.source_code,
            config.shots.source_code,
            config.xg.source_code,
            config.shots.source_code,
        ],
    ).fetchall()
    values = {}
    for row in rows:
        if any(count > 1 for count in row[5:9]):
            raise RichRateResearchError(
                f"Duplicate provider performance rows for fixture {row[0]}"
            )
        home_xg, away_xg = (row[1], row[2]) if row[5] == row[6] == 1 else (None, None)
        home_shots, away_shots = (
            (row[3], row[4]) if row[7] == row[8] == 1 else (None, None)
        )
        kickoff = row[9]
        contributing_retrievals = []
        if home_xg is not None:
            contributing_retrievals.append(row[10])
        if home_shots is not None:
            contributing_retrievals.append(row[11])
        source_max_retrieved_at = (
            max(contributing_retrievals) if contributing_retrievals else None
        )
        strict_forward_fixture = (
            strict_retrieval_from is not None
            and kickoff >= strict_retrieval_from
        )
        available_at = None
        if strict_forward_fixture and source_max_retrieved_at is not None:
            available_at = max(
                kickoff
                + timedelta(minutes=config.result_availability_delay_minutes),
                source_max_retrieved_at,
            )
        if any(
            value is not None and value < 0
            for value in (home_xg, away_xg, home_shots, away_shots)
        ):
            raise RichRateResearchError(
                f"Negative performance value for fixture {row[0]}"
            )
        values[str(row[0])] = FixturePerformance(
            fixture_id=str(row[0]),
            home_xg=home_xg,
            away_xg=away_xg,
            home_shots=home_shots,
            away_shots=away_shots,
            available_at=available_at,
            source_max_retrieved_at=(
                source_max_retrieved_at if strict_forward_fixture else None
            ),
        )
    return values


class ChronologicalRichRateBuilder:
    def __init__(self, config: RichRateConfig) -> None:
        self.config = config
        self.teams: dict[str, _RichTeamState] = {}

    def build(
        self,
        base_rows: list[RegulationFeatureRow],
        performance: dict[str, FixturePerformance],
    ) -> list[RichRateFeatureRow]:
        self.teams = {}
        snapshots: dict[datetime, list[RegulationFeatureRow]] = defaultdict(list)
        targets: dict[str, RegulationFeatureRow] = {}
        for row in base_rows:
            snapshots[row.prediction_at].append(row)
            targets.setdefault(row.fixture_id, row)
        results: dict[datetime, list[RegulationFeatureRow]] = defaultdict(list)
        for target in targets.values():
            results[
                target.kickoff
                + timedelta(minutes=self.config.result_availability_delay_minutes)
            ].append(target)
        output = []
        for at in sorted(set(snapshots) | set(results)):
            for row in sorted(
                snapshots.get(at, []),
                key=lambda item: (item.fixture_id, item.information_state),
            ):
                output.append(self._snapshot(row, at))
            self._apply_batch(results.get(at, []), performance, at)
        return sorted(
            output,
            key=lambda row: (row.kickoff, row.fixture_id, row.information_state),
        )

    def build_inference(
        self,
        historical_base_rows: list[RegulationFeatureRow],
        inference_rows: list[RegulationInferenceFeatureRow],
        performance: dict[str, FixturePerformance],
    ) -> list[RichRateInferenceFeatureRow]:
        self.teams = {}
        if not inference_rows:
            return []
        snapshots: dict[datetime, list[RegulationInferenceFeatureRow]] = (
            defaultdict(list)
        )
        for row in inference_rows:
            snapshots[row.prediction_at].append(row)
        historical_targets: dict[str, RegulationFeatureRow] = {}
        for row in historical_base_rows:
            historical_targets.setdefault(row.fixture_id, row)
        latest_snapshot = max(snapshots)
        results: dict[datetime, list[RegulationFeatureRow]] = defaultdict(list)
        for target in historical_targets.values():
            observed = performance.get(target.fixture_id)
            available_at = (
                observed.available_at
                if observed is not None and observed.available_at is not None
                else target.kickoff
                + timedelta(minutes=self.config.result_availability_delay_minutes)
            )
            if available_at <= latest_snapshot:
                results[available_at].append(target)
        output = []
        for at in sorted(set(snapshots) | set(results)):
            for row in sorted(
                snapshots.get(at, []),
                key=lambda item: (item.fixture_id, item.information_state),
            ):
                output.append(self._snapshot_inference(row, at))
            self._apply_batch(results.get(at, []), performance, at)
        return sorted(
            output,
            key=lambda row: (row.kickoff, row.fixture_id, row.information_state),
        )

    def _new_team(self) -> _RichTeamState:
        def state(metric: MetricConfig) -> _EvidenceState:
            return _EvidenceState(metric.prior_mean, metric.prior_strength)

        return _RichTeamState(
            xg_attack=state(self.config.xg),
            xg_defense=state(self.config.xg),
            shots_attack=state(self.config.shots),
            shots_defense=state(self.config.shots),
        )

    def _project(self, team_id: str, at: datetime) -> _RichTeamState:
        team = self.teams.setdefault(team_id, self._new_team())
        for state in (team.xg_attack, team.xg_defense):
            state.project(at, self.config.xg.half_life_days)
        for state in (team.shots_attack, team.shots_defense):
            state.project(at, self.config.shots.half_life_days)
        return team

    def _snapshot(self, row: RegulationFeatureRow, at: datetime) -> RichRateFeatureRow:
        home = self._project(row.home_team_id, at)
        away = self._project(row.away_team_id, at)
        return RichRateFeatureRow(
            feature_version=self.config.feature_version,
            fixture_id=row.fixture_id,
            information_state=row.information_state,
            prediction_at=row.prediction_at,
            kickoff=row.kickoff,
            home_team_id=row.home_team_id,
            away_team_id=row.away_team_id,
            home_xg_attack=home.xg_attack.mean,
            home_xg_defense=home.xg_defense.mean,
            away_xg_attack=away.xg_attack.mean,
            away_xg_defense=away.xg_defense.mean,
            home_shots_attack=home.shots_attack.mean,
            home_shots_defense=home.shots_defense.mean,
            away_shots_attack=away.shots_attack.mean,
            away_shots_defense=away.shots_defense.mean,
            home_xg_history=home.xg_history,
            away_xg_history=away.xg_history,
            home_shots_history=home.shots_history,
            away_shots_history=away.shots_history,
        )

    def _snapshot_inference(
        self, row: RegulationInferenceFeatureRow, at: datetime
    ) -> RichRateInferenceFeatureRow:
        base = self._snapshot(row, at)
        home = self.teams[row.home_team_id]
        away = self.teams[row.away_team_id]
        return RichRateInferenceFeatureRow(
            **asdict(base),
            source_max_retrieved_at=_maximum_timestamp(
                home.source_max_retrieved_at,
                away.source_max_retrieved_at,
            ),
        )

    def _apply_batch(
        self,
        rows: list[RegulationFeatureRow],
        performance: dict[str, FixturePerformance],
        at: datetime,
    ) -> None:
        projected = []
        for row in sorted(rows, key=lambda item: item.fixture_id):
            projected.append(
                (
                    row,
                    self._project(row.home_team_id, at),
                    self._project(row.away_team_id, at),
                    performance.get(row.fixture_id),
                )
            )
        for _, home, away, observed in projected:
            if observed is None:
                continue
            if observed.home_xg is not None and observed.away_xg is not None:
                home.xg_attack.update(observed.home_xg)
                away.xg_defense.update(observed.home_xg)
                away.xg_attack.update(observed.away_xg)
                home.xg_defense.update(observed.away_xg)
                home.xg_history += 1
                away.xg_history += 1
            if observed.home_shots is not None and observed.away_shots is not None:
                home.shots_attack.update(observed.home_shots)
                away.shots_defense.update(observed.home_shots)
                away.shots_attack.update(observed.away_shots)
                home.shots_defense.update(observed.away_shots)
                home.shots_history += 1
                away.shots_history += 1
            if observed.source_max_retrieved_at is not None:
                home.source_max_retrieved_at = _maximum_timestamp(
                    home.source_max_retrieved_at,
                    observed.source_max_retrieved_at,
                )
                away.source_max_retrieved_at = _maximum_timestamp(
                    away.source_max_retrieved_at,
                    observed.source_max_retrieved_at,
                )


def _maximum_timestamp(*values: datetime | None) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def research_rich_rate_candidate(
    rich_rows: list[RichRateFeatureRow],
    baseline_predictions: list[WalkForwardPrediction],
    *,
    config: RichRateConfig,
    walk_forward_config: WalkForwardConfig,
) -> tuple[list[RichRateFit], list[RichRatePrediction], dict]:
    if config.validation_end_exclusive > min(
        fold.kickoff_end_exclusive
        for fold in walk_forward_config.folds
        if fold.fold_key == "development" and fold.kickoff_end_exclusive is not None
    ):
        raise RichRateResearchError("Rich-rate validation leaves development fold")
    rich = {(row.fixture_id, row.information_state): row for row in rich_rows}
    grouped: dict[str, list[WalkForwardPrediction]] = defaultdict(list)
    for row in baseline_predictions:
        if row.model_key == "independent_poisson" and row.fold_key == "development":
            grouped[row.information_state].append(row)
    fits = []
    predictions = []
    for information_state, values in sorted(grouped.items()):
        fit_rows = [row for row in values if row.kickoff < config.fit_end_exclusive]
        validation_rows = [
            row
            for row in values
            if config.fit_end_exclusive <= row.kickoff < config.validation_end_exclusive
        ]
        if len(fit_rows) < config.minimum_fit_fixtures:
            raise RichRateResearchError(
                f"Insufficient rich-rate fit rows for {information_state}: {len(fit_rows)}"
            )
        beta, converged, iterations = _fit_poisson_correction(fit_rows, rich, config)
        fits.append(
            RichRateFit(
                information_state=information_state,
                fit_fixtures=len(fit_rows),
                fit_kickoff_end_exclusive=config.fit_end_exclusive,
                coefficients={"xg_signal": beta[0], "shots_signal": beta[1]},
                converged=converged,
                iterations=iterations,
            )
        )
        for row in validation_rows:
            feature = rich[(row.fixture_id, row.information_state)]
            predictions.append(_score_candidate(row, feature, beta, config))
    summary = _summarize_candidate(predictions, config, walk_forward_config)
    return fits, sorted(predictions, key=lambda row: (row.kickoff, row.fixture_id)), summary


def evaluate_promoted_rich_rate_candidate(
    rich_rows: list[RichRateFeatureRow],
    baseline_predictions: list[WalkForwardPrediction],
    *,
    config: RichRateConfig,
    walk_forward_config: WalkForwardConfig,
    selection_evidence: dict,
) -> tuple[list[RichRateFit], list[RichRatePrediction]]:
    _validate_selection_evidence(selection_evidence)
    development_ends = [
        fold.kickoff_end_exclusive
        for fold in walk_forward_config.folds
        if fold.fold_key == "development"
    ]
    if len(development_ends) != 1 or development_ends[0] is None:
        raise RichRateResearchError("Exactly one bounded development fold is required")
    development_end = development_ends[0]
    rich = {(row.fixture_id, row.information_state): row for row in rich_rows}
    grouped: dict[str, list[WalkForwardPrediction]] = defaultdict(list)
    for row in baseline_predictions:
        if row.model_key == "independent_poisson":
            grouped[row.information_state].append(row)
    fits = []
    predictions = []
    for information_state, values in sorted(grouped.items()):
        fit_rows = [row for row in values if row.fold_key == "development"]
        if len(fit_rows) < config.minimum_fit_fixtures:
            raise RichRateResearchError(
                f"Insufficient promoted fit rows for {information_state}: "
                f"{len(fit_rows)}"
            )
        beta, converged, iterations = _fit_poisson_correction(
            fit_rows, rich, config
        )
        if not converged:
            raise RichRateResearchError(
                f"Promoted rich-rate fit did not converge for {information_state}"
            )
        fits.append(
            RichRateFit(
                information_state=information_state,
                fit_fixtures=len(fit_rows),
                fit_kickoff_end_exclusive=development_end,
                coefficients={"xg_signal": beta[0], "shots_signal": beta[1]},
                converged=converged,
                iterations=iterations,
            )
        )
        for row in values:
            if row.fold_key in {
                walk_forward_config.calibration_fit_fold,
                walk_forward_config.calibration_apply_fold,
            }:
                feature = rich[(row.fixture_id, row.information_state)]
                predictions.append(_score_candidate(row, feature, beta, config))
    return fits, sorted(
        predictions,
        key=lambda row: (
            row.prediction_at,
            row.fixture_id,
            row.information_state,
        ),
    )


def rich_feature_rows_sha256(rows: list[RichRateFeatureRow]) -> str:
    values = []
    for row in rows:
        value = asdict(row)
        value["prediction_at"] = row.prediction_at.astimezone(timezone.utc).isoformat()
        value["kickoff"] = row.kickoff.astimezone(timezone.utc).isoformat()
        values.append(value)
    body = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def fit_rich_rate_coefficients(
    rows: list[object],
    rich_rows: list[RichRateFeatureRow],
    config: RichRateConfig,
) -> tuple[dict[str, float], bool, int]:
    rich = {(row.fixture_id, row.information_state): row for row in rich_rows}
    beta, converged, iterations = _fit_poisson_correction(rows, rich, config)
    return (
        {"xg_signal": beta[0], "shots_signal": beta[1]},
        converged,
        iterations,
    )


def apply_rich_rate_correction(
    base_home_rate: float,
    base_away_rate: float,
    rich_row: RichRateFeatureRow,
    coefficients: dict[str, float],
    config: RichRateConfig,
) -> tuple[float, float]:
    beta = [coefficients["xg_signal"], coefficients["shots_signal"]]
    home_signal, away_signal = _signals(rich_row, config)
    home_rate = _clamp(
        base_home_rate
        * math.exp(sum(b * x for b, x in zip(beta, home_signal))),
        config.minimum_expected_goals,
        config.maximum_expected_goals,
    )
    away_rate = _clamp(
        base_away_rate
        * math.exp(sum(b * x for b, x in zip(beta, away_signal))),
        config.minimum_expected_goals,
        config.maximum_expected_goals,
    )
    return home_rate, away_rate


def summarize_promoted_rich_rate(
    raw_predictions: list[RichRatePrediction],
    calibrated_predictions: list[object],
    baseline_predictions: list[WalkForwardPrediction],
    calibrated_baseline_predictions: list[object],
    walk_forward_config: WalkForwardConfig,
) -> dict:
    test_fold = walk_forward_config.calibration_apply_fold
    raw_baseline = {
        (row.fixture_id, row.information_state): row
        for row in baseline_predictions
        if row.model_key == "independent_poisson" and row.fold_key == test_fold
    }
    calibrated_baseline = {
        (row.fixture_id, row.information_state): row
        for row in calibrated_baseline_predictions
        if row.model_key == "independent_poisson_temperature_calibrated"
        and row.fold_key == test_fold
    }
    raw_by_key = {
        (row.fixture_id, row.information_state): row
        for row in raw_predictions
        if row.fold_key == test_fold
    }
    grouped: dict[str, list[object]] = defaultdict(list)
    for row in calibrated_predictions:
        if row.fold_key == test_fold:
            grouped[row.information_state].append(row)
    metrics = []
    comparisons = []
    for information_state, values in sorted(grouped.items()):
        metrics.append(
            {
                "model_key": values[0].model_key,
                "information_state": information_state,
                "fold_key": test_fold,
                "fixtures": len(values),
                "mean_moneyline_log_loss": math.fsum(
                    row.moneyline_log_loss for row in values
                )
                / len(values),
                "mean_moneyline_brier": math.fsum(
                    row.moneyline_brier for row in values
                )
                / len(values),
            }
        )
        pairs = [
            (
                row,
                calibrated_baseline[(row.fixture_id, row.information_state)],
            )
            for row in values
            if (row.fixture_id, row.information_state) in calibrated_baseline
        ]
        comparisons.extend(
            _paired_prediction_comparisons(
                pairs,
                information_state=information_state,
                fold_key=test_fold,
                challenger_model=values[0].model_key,
                baseline_model="independent_poisson_temperature_calibrated",
                walk=walk_forward_config,
            )
        )
        raw_pairs = [
            (row, raw_baseline[key])
            for key, row in raw_by_key.items()
            if key[1] == information_state and key in raw_baseline
        ]
        comparisons.extend(
            _paired_prediction_comparisons(
                raw_pairs,
                information_state=information_state,
                fold_key=test_fold,
                challenger_model=(
                    "independent_poisson_xg_shots_correction_v1"
                ),
                baseline_model="independent_poisson",
                walk=walk_forward_config,
            )
        )
    return {
        "selection_policy": "development_internal_validation_gate",
        "coefficient_refit_fold": "development",
        "calibrator_fit_fold": walk_forward_config.calibration_fit_fold,
        "final_evaluation_fold": test_fold,
        "metrics": metrics,
        "paired_model_comparisons": comparisons,
    }


def _paired_prediction_comparisons(
    pairs: list[tuple[object, object]],
    *,
    information_state: str,
    fold_key: str,
    challenger_model: str,
    baseline_model: str,
    walk: WalkForwardConfig,
) -> list[dict]:
    output = []
    for metric in ("moneyline_log_loss", "moneyline_brier"):
        blocks: dict[tuple[int, int], list[float]] = defaultdict(list)
        for challenger, baseline in pairs:
            blocks[(challenger.kickoff.year, challenger.kickoff.month)].append(
                getattr(challenger, metric) - getattr(baseline, metric)
            )
        differences = [value for block in blocks.values() for value in block]
        lower, upper, probability = block_bootstrap_interval(
            blocks,
            replicates=walk.bootstrap_replicates,
            seed=comparison_seed(
                walk.bootstrap_seed,
                challenger_model,
                baseline_model,
                information_state,
                fold_key,
                metric,
            ),
        )
        output.append(
            {
                "challenger_model": challenger_model,
                "baseline_model": baseline_model,
                "information_state": information_state,
                "fold_key": fold_key,
                "metric": metric,
                "fixtures": len(differences),
                "calendar_month_blocks": len(blocks),
                "mean_delta_challenger_minus_baseline": math.fsum(differences)
                / len(differences),
                "paired_month_block_bootstrap_95_lower": lower,
                "paired_month_block_bootstrap_95_upper": upper,
                "bootstrap_probability_challenger_is_better": probability,
                "lower_is_better": True,
            }
        )
    return output


def _fit_poisson_correction(
    rows: list[WalkForwardPrediction],
    rich: dict[tuple[str, str], RichRateFeatureRow],
    config: RichRateConfig,
) -> tuple[list[float], bool, int]:
    instances = []
    for row in rows:
        feature = rich[(row.fixture_id, row.information_state)]
        home, away = _signals(feature, config)
        instances.append((row.expected_home_goals, row.home_goals, home))
        instances.append((row.expected_away_goals, row.away_goals, away))
    beta = [0.0, 0.0]
    converged = False
    for iteration in range(1, config.maximum_newton_iterations + 1):
        gradient = [-config.ridge_penalty * value for value in beta]
        information = [
            [config.ridge_penalty if i == j else 0.0 for j in range(2)]
            for i in range(2)
        ]
        for base_rate, goals, signal in instances:
            rate = base_rate * math.exp(sum(b * x for b, x in zip(beta, signal)))
            residual = goals - rate
            for i in range(2):
                gradient[i] += signal[i] * residual
                for j in range(2):
                    information[i][j] += rate * signal[i] * signal[j]
        step = _solve(information, gradient)
        if max(abs(value) for value in step) < config.optimizer_tolerance:
            converged = True
            return beta, converged, iteration
        current = _poisson_objective(instances, beta, config.ridge_penalty)
        scale = 1.0
        while scale >= 1e-6:
            candidate = [value + scale * change for value, change in zip(beta, step)]
            if _poisson_objective(instances, candidate, config.ridge_penalty) < current:
                beta = candidate
                break
            scale *= 0.5
        else:
            return beta, False, iteration
    return beta, converged, config.maximum_newton_iterations


def _poisson_objective(instances, beta: list[float], ridge: float) -> float:
    return math.fsum(
        base_rate * math.exp(sum(b * x for b, x in zip(beta, signal)))
        - goals * (math.log(base_rate) + sum(b * x for b, x in zip(beta, signal)))
        for base_rate, goals, signal in instances
    ) + 0.5 * ridge * math.fsum(value * value for value in beta)


def _signals(
    row: RichRateFeatureRow, config: RichRateConfig
) -> tuple[tuple[float, float], tuple[float, float]]:
    home_xg = _signal(
        row.home_xg_attack,
        row.away_xg_defense,
        min(row.home_xg_history, row.away_xg_history),
        config.xg.prior_mean,
        config.full_signal_history_matches,
    )
    away_xg = _signal(
        row.away_xg_attack,
        row.home_xg_defense,
        min(row.away_xg_history, row.home_xg_history),
        config.xg.prior_mean,
        config.full_signal_history_matches,
    )
    home_shots = _signal(
        row.home_shots_attack,
        row.away_shots_defense,
        min(row.home_shots_history, row.away_shots_history),
        config.shots.prior_mean,
        config.full_signal_history_matches,
    )
    away_shots = _signal(
        row.away_shots_attack,
        row.home_shots_defense,
        min(row.away_shots_history, row.home_shots_history),
        config.shots.prior_mean,
        config.full_signal_history_matches,
    )
    return (home_xg, home_shots), (away_xg, away_shots)


def _signal(
    attack: float,
    defense: float,
    history: int,
    prior: float,
    full_history: int,
) -> float:
    coverage = min(history / full_history, 1.0)
    return coverage * math.log(max((attack + defense) / 2.0, 1e-6) / prior)


def _score_candidate(
    base: WalkForwardPrediction,
    rich: RichRateFeatureRow,
    beta: list[float],
    config: RichRateConfig,
) -> RichRatePrediction:
    home_signal, away_signal = _signals(rich, config)
    home_rate = _clamp(
        base.expected_home_goals
        * math.exp(sum(b * x for b, x in zip(beta, home_signal))),
        config.minimum_expected_goals,
        config.maximum_expected_goals,
    )
    away_rate = _clamp(
        base.expected_away_goals
        * math.exp(sum(b * x for b, x in zip(beta, away_signal))),
        config.minimum_expected_goals,
        config.maximum_expected_goals,
    )
    probabilities, _ = moneyline_probabilities(
        home_rate, away_rate, 0.0, 1e-12
    )
    log_loss = -math.log(probabilities[base.result])
    brier = math.fsum(
        (probabilities[key] - float(key == base.result)) ** 2
        for key in ("home_win", "draw", "away_win")
    )
    return RichRatePrediction(
        model_key="independent_poisson_xg_shots_correction_v1",
        fixture_id=base.fixture_id,
        information_state=base.information_state,
        prediction_at=base.prediction_at,
        kickoff=base.kickoff,
        fold_key=base.fold_key,
        result=base.result,
        expected_home_goals=home_rate,
        expected_away_goals=away_rate,
        home_win_probability=probabilities["home_win"],
        draw_probability=probabilities["draw"],
        away_win_probability=probabilities["away_win"],
        moneyline_log_loss=log_loss,
        moneyline_brier=brier,
        base_moneyline_log_loss=base.moneyline_log_loss,
        base_moneyline_brier=base.moneyline_brier,
    )


def _validate_selection_evidence(summary: dict) -> None:
    if summary.get("research_scope") != "development_only":
        raise RichRateResearchError("Promotion requires development-only research")
    if summary.get("test_fold_accessed") is not False:
        raise RichRateResearchError("Selection evidence accessed the test fold")
    metrics = summary.get("metrics", [])
    if not metrics:
        raise RichRateResearchError("Selection evidence has no metrics")
    for item in metrics:
        for metric in ("moneyline_log_loss", "moneyline_brier"):
            upper = item.get(metric, {}).get(
                "paired_month_block_bootstrap_95_upper"
            )
            if upper is None or upper >= 0:
                raise RichRateResearchError(
                    f"Rich-rate promotion gate failed for "
                    f"{item.get('information_state')}/{metric}"
                )


def _summarize_candidate(
    rows: list[RichRatePrediction],
    config: RichRateConfig,
    walk: WalkForwardConfig,
) -> dict:
    grouped: dict[str, list[RichRatePrediction]] = defaultdict(list)
    for row in rows:
        grouped[row.information_state].append(row)
    metrics = []
    for information_state, values in sorted(grouped.items()):
        item = {
            "information_state": information_state,
            "fixtures": len(values),
            "validation_start": config.fit_end_exclusive.isoformat(),
            "validation_end_exclusive": config.validation_end_exclusive.isoformat(),
            "test_fold_accessed": False,
        }
        for metric, base_metric in (
            ("moneyline_log_loss", "base_moneyline_log_loss"),
            ("moneyline_brier", "base_moneyline_brier"),
        ):
            differences = [
                getattr(row, metric) - getattr(row, base_metric) for row in values
            ]
            blocks: dict[tuple[int, int], list[float]] = defaultdict(list)
            for row, difference in zip(values, differences):
                blocks[(row.kickoff.year, row.kickoff.month)].append(difference)
            lower, upper, probability = block_bootstrap_interval(
                blocks,
                replicates=walk.bootstrap_replicates,
                seed=comparison_seed(
                    walk.bootstrap_seed, "rich_rate", information_state, metric
                ),
            )
            item[metric] = {
                "candidate_mean": math.fsum(getattr(row, metric) for row in values)
                / len(values),
                "baseline_mean": math.fsum(
                    getattr(row, base_metric) for row in values
                )
                / len(values),
                "mean_delta_candidate_minus_baseline": math.fsum(differences)
                / len(differences),
                "paired_month_block_bootstrap_95_lower": lower,
                "paired_month_block_bootstrap_95_upper": upper,
                "bootstrap_probability_candidate_is_better": probability,
            }
        metrics.append(item)
    return {
        "research_scope": "development_only",
        "fit_end_exclusive": config.fit_end_exclusive.isoformat(),
        "validation_end_exclusive": config.validation_end_exclusive.isoformat(),
        "test_fold_accessed": False,
        "metrics": metrics,
    }


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [matrix[i][:] + [vector[i]] for i in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise RichRateResearchError("Singular rich-rate information matrix")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[i][-1] for i in range(size)]


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)
