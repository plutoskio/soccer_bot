import os
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from apps.api.snapshot_store import (
    DEFAULT_SNAPSHOT_PATH,
    S3SnapshotStore,
    SnapshotStore,
    SnapshotUnavailableError,
    SnapshotValidationError,
    snapshot_age_seconds,
)


InformationState = Literal["pre_lineup_72h_clean_v1", "pre_lineup_24h_v1"]
Selection = Literal["home_win", "draw", "away_win"]


class PriceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    information_state: InformationState
    contract_key: Literal["regulation_moneyline"]
    selection: Selection


class PriceResponse(BaseModel):
    fixture_id: str
    information_state: InformationState
    contract_key: Literal["regulation_moneyline"]
    selection: Selection
    probability: float
    fair_decimal_odds: float
    model_version: str
    prediction_at: str
    snapshot_as_of: str


def create_app(store: SnapshotStore | None = None) -> FastAPI:
    snapshot_store = store or _store_from_environment()

    app = FastAPI(
        title="Soccer Bot Prediction API",
        version="1.0.0",
        description=(
            "Read-only access to immutable, leakage-safe Soccer Bot prediction "
            "snapshots. Only calibrated regulation moneyline is currently priced."
        ),
    )
    app.state.snapshot_store = snapshot_store

    @app.exception_handler(SnapshotUnavailableError)
    async def unavailable_handler(
        _request: Request, exc: SnapshotUnavailableError
    ) -> Any:
        return _error_response(503, "snapshot_unavailable", str(exc))

    @app.exception_handler(SnapshotValidationError)
    async def invalid_handler(
        _request: Request, exc: SnapshotValidationError
    ) -> Any:
        return _error_response(503, "snapshot_invalid", str(exc))

    def get_store(request: Request) -> SnapshotStore:
        return request.app.state.snapshot_store

    StoreDependency = Annotated[SnapshotStore, Depends(get_store)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "soccer-bot-api"}

    @app.get("/ready")
    def readiness(store: StoreDependency) -> dict[str, Any]:
        snapshot = store.load()
        age_seconds = snapshot_age_seconds(snapshot)
        stale_after = int(os.environ.get("SOCCER_SNAPSHOT_STALE_SECONDS", "21600"))
        return {
            "status": "stale" if age_seconds > stale_after else "ok",
            "snapshot_as_of": snapshot["as_of"],
            "snapshot_age_seconds": round(age_seconds),
            "model_version": snapshot["model_version"],
            "fixture_count": snapshot["fixture_count"],
        }

    @app.get("/v1/snapshot")
    def get_snapshot(store: StoreDependency) -> dict[str, Any]:
        snapshot = store.load()
        age_seconds = snapshot_age_seconds(snapshot)
        stale_after = int(os.environ.get("SOCCER_SNAPSHOT_STALE_SECONDS", "21600"))
        return {
            **snapshot,
            "snapshot_age_seconds": round(age_seconds),
            "is_stale": age_seconds > stale_after,
        }

    @app.get("/v1/fixtures")
    def get_fixtures(
        store: StoreDependency,
        information_state: Annotated[InformationState | None, Query()] = None,
    ) -> dict[str, Any]:
        snapshot = store.load()
        predictions = snapshot["predictions"]
        if information_state is not None:
            predictions = [
                row
                for row in predictions
                if row["information_state"] == information_state
            ]
        return {
            "as_of": snapshot["as_of"],
            "model_version": snapshot["model_version"],
            "predictions": predictions,
        }

    @app.get("/v1/fixtures/{fixture_id}")
    def get_fixture(fixture_id: str, store: StoreDependency) -> dict[str, Any]:
        snapshot = store.load()
        predictions = [
            row for row in snapshot["predictions"] if row["fixture_id"] == fixture_id
        ]
        if not predictions:
            raise HTTPException(status_code=404, detail="fixture_not_found")
        return {
            "as_of": snapshot["as_of"],
            "model_version": snapshot["model_version"],
            "predictions": predictions,
        }

    @app.post("/v1/price", response_model=PriceResponse)
    def price_contract(request: PriceRequest, store: StoreDependency) -> PriceResponse:
        snapshot = store.load()
        prediction = next(
            (
                row
                for row in snapshot["predictions"]
                if row["fixture_id"] == request.fixture_id
                and row["information_state"] == request.information_state
            ),
            None,
        )
        if prediction is None:
            raise HTTPException(status_code=404, detail="prediction_not_found")
        field = {
            "home_win": "home_win_probability",
            "draw": "draw_probability",
            "away_win": "away_win_probability",
        }[request.selection]
        probability = prediction[field]
        return PriceResponse(
            fixture_id=request.fixture_id,
            information_state=request.information_state,
            contract_key=request.contract_key,
            selection=request.selection,
            probability=probability,
            fair_decimal_odds=round(1.0 / probability, 4),
            model_version=snapshot["model_version"],
            prediction_at=prediction["prediction_at"],
            snapshot_as_of=snapshot["as_of"],
        )

    return app


def _error_response(status_code: int, code: str, message: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
    )


def _store_from_environment():
    bucket = os.environ.get("SOCCER_SNAPSHOT_S3_BUCKET")
    if not bucket:
        return SnapshotStore(
            Path(os.environ.get("SOCCER_SNAPSHOT_PATH", DEFAULT_SNAPSHOT_PATH))
        )
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=os.environ.get("SOCCER_SNAPSHOT_S3_ENDPOINT"),
        region_name=os.environ.get("AWS_DEFAULT_REGION", "auto"),
    )
    return S3SnapshotStore(
        client=client,
        bucket=bucket,
        key=os.environ.get(
            "SOCCER_SNAPSHOT_S3_KEY",
            "regulation_champion_v1/latest.json",
        ),
        cache_seconds=float(os.environ.get("SOCCER_SNAPSHOT_CACHE_SECONDS", "30")),
    )


app = create_app()
