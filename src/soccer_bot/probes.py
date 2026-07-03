from __future__ import annotations

from dataclasses import dataclass
import csv
import io
import json
from pathlib import Path
import time
from typing import Iterable

from .http import HttpClient, HttpResponse
from .raw_store import RawArtifactStore, StoredArtifact


FINAL_STATUSES = {"FT", "AET", "PEN"}
UPCOMING_STATUSES = {"NS", "TBD", "PST"}


@dataclass(frozen=True)
class ProbeResult:
    source: str
    resource: str
    status: int
    artifact: StoredArtifact
    result_count: int | None
    note: str = ""


def _result_count(payload) -> int | None:
    if isinstance(payload, dict):
        if isinstance(payload.get("results"), int):
            return payload["results"]
        response = payload.get("response")
        if isinstance(response, list):
            return len(response)
        events = payload.get("events")
        if isinstance(events, list):
            return len(events)
        players = payload.get("players")
        if isinstance(players, list):
            return len(players)
    if isinstance(payload, list):
        return len(payload)
    return None


class BaseProbe:
    def __init__(self, client: HttpClient, store: RawArtifactStore, max_calls: int) -> None:
        self.client = client
        self.store = store
        self.max_calls = max_calls
        self.calls = 0
        self.results: list[ProbeResult] = []

    def _record(
        self,
        source: str,
        resource: str,
        response: HttpResponse,
        params: dict[str, object] | None = None,
        *,
        count_against_budget: bool = True,
    ) -> object:
        if count_against_budget:
            self.calls += 1
        artifact = self.store.store(
            source=source,
            resource=resource,
            response=response,
            request_params=params,
        )
        try:
            payload = response.json()
            note = ""
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            payload = None
            note = f"invalid_json:{type(error).__name__}"
        self.results.append(
            ProbeResult(source, resource, response.status, artifact, _result_count(payload), note)
        )
        return payload

    def _check_budget(self) -> None:
        if self.calls >= self.max_calls:
            raise RuntimeError(f"Probe call budget exhausted ({self.calls}/{self.max_calls})")


class ApiFootballProbe(BaseProbe):
    BASE_URL = "https://v3.football.api-sports.io"

    def __init__(
        self,
        client: HttpClient,
        store: RawArtifactStore,
        api_key: str,
        max_calls: int,
        minimum_interval_seconds: float,
    ) -> None:
        super().__init__(client, store, max_calls)
        if not api_key:
            raise ValueError("API_FOOTBALL_KEY is missing")
        self.headers = {"x-apisports-key": api_key}
        self.minimum_interval_seconds = minimum_interval_seconds
        self.last_request_at: float | None = None
        self.rate_limited = False

    def get(self, resource: str, path: str, params: dict[str, object] | None = None):
        if self.rate_limited:
            return None
        self._check_budget()
        if self.last_request_at is not None:
            wait_seconds = self.minimum_interval_seconds - (
                time.monotonic() - self.last_request_at
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        response = self.client.get(
            self.BASE_URL, path, params=params, headers=self.headers
        )
        self.last_request_at = time.monotonic()
        payload = self._record("api_football", resource, response, params)
        if response.status == 429:
            self.rate_limited = True
        return payload

    def run(
        self,
        dates: Iterable[str],
        timezone_name: str,
        max_detail_fixtures: int,
        fixture_ids: Iterable[int] | None = None,
    ) -> list[ProbeResult]:
        # API-Football documents /status as not counting against the daily quota.
        status_response = self.client.get(self.BASE_URL, "/status", headers=self.headers)
        status_payload = self._record(
            "api_football",
            "status",
            status_response,
            count_against_budget=False,
        )
        if status_response.status != 200:
            raise RuntimeError(f"API-Football authentication failed: HTTP {status_response.status}")
        if isinstance(status_payload, dict) and status_payload.get("errors"):
            raise RuntimeError("API-Football status endpoint returned an API error")
        self.last_request_at = time.monotonic()

        selected: list[dict] = []
        details_already_fetched = bool(fixture_ids)
        if fixture_ids:
            for fixture_id in list(fixture_ids)[:max_detail_fixtures]:
                payload = self.get("fixture_by_id", "/fixtures", {"id": fixture_id})
                if isinstance(payload, dict) and isinstance(payload.get("response"), list):
                    selected.extend(
                        item for item in payload["response"] if isinstance(item, dict)
                    )
                if self.rate_limited:
                    return self.results
        else:
            fixtures: list[dict] = []
            for date in dates:
                payload = self.get(
                    "fixtures_by_date",
                    "/fixtures",
                    {"date": date, "timezone": timezone_name},
                )
                if isinstance(payload, dict) and isinstance(payload.get("response"), list):
                    fixtures.extend(
                        item for item in payload["response"] if isinstance(item, dict)
                    )
                if self.rate_limited:
                    return self.results
            selected = self._select_fixtures(fixtures, max_detail_fixtures)

        for item in selected:
            fixture_id = item.get("fixture", {}).get("id")
            status = item.get("fixture", {}).get("status", {}).get("short")
            if not fixture_id:
                continue
            fixture_param = {"fixture": fixture_id}
            if not details_already_fetched:
                self.get("fixture_by_id", "/fixtures", {"id": fixture_id})
                if self.rate_limited:
                    break
            self.get("fixture_lineups", "/fixtures/lineups", fixture_param)
            if self.rate_limited:
                break
            self.get("fixture_events", "/fixtures/events", fixture_param)
            if self.rate_limited:
                break
            if status in FINAL_STATUSES:
                self.get("fixture_players", "/fixtures/players", fixture_param)
                if self.rate_limited:
                    break
                self.get("fixture_statistics", "/fixtures/statistics", fixture_param)
            elif status in UPCOMING_STATUSES:
                self.get("fixture_injuries", "/injuries", fixture_param)
            if self.rate_limited:
                break
        return self.results

    @staticmethod
    def _select_fixtures(fixtures: list[dict], limit: int) -> list[dict]:
        def status_of(item: dict) -> str | None:
            return item.get("fixture", {}).get("status", {}).get("short")

        priority_terms = (
            "world cup",
            "champions league",
            "uefa",
            "europa league",
            "premier league",
            "la liga",
            "serie a",
            "bundesliga",
            "ligue 1",
            "major league soccer",
        )

        def competition_rank(item: dict) -> int:
            name = str(item.get("league", {}).get("name", "")).lower()
            for index, term in enumerate(priority_terms):
                if term in name:
                    return len(priority_terms) - index
            return 0

        final = sorted(
            (item for item in fixtures if status_of(item) in FINAL_STATUSES),
            key=competition_rank,
            reverse=True,
        )
        upcoming = sorted(
            (item for item in fixtures if status_of(item) in UPCOMING_STATUSES),
            key=competition_rank,
            reverse=True,
        )
        selected: list[dict] = []
        if final:
            selected.append(final[0])
        if upcoming and len(selected) < limit:
            selected.append(upcoming[0])
        seen = {item.get("fixture", {}).get("id") for item in selected}
        for item in fixtures:
            fixture_id = item.get("fixture", {}).get("id")
            if fixture_id not in seen and len(selected) < limit:
                selected.append(item)
                seen.add(fixture_id)
        return selected


class PolymarketProbe(BaseProbe):
    GAMMA_URL = "https://gamma-api.polymarket.com"
    CLOB_URL = "https://clob.polymarket.com"
    SOCCER_HINTS = {
        "soccer",
        "football",
        "epl",
        "premier league",
        "la liga",
        "serie a",
        "bundesliga",
        "ligue 1",
        "champions league",
        "ucl",
        "uefa",
        "fifa",
        "world cup",
        "mls",
    }

    def get(self, resource: str, path: str, params: dict[str, object] | None = None):
        self._check_budget()
        response = self.client.get(self.GAMMA_URL, path, params=params)
        return self._record("polymarket_gamma", resource, response, params)

    def get_clob(self, resource: str, path: str, params: dict[str, object] | None = None):
        self._check_budget()
        response = self.client.get(self.CLOB_URL, path, params=params)
        return self._record("polymarket_clob", resource, response, params)

    def run(
        self,
        events_per_tag: int,
        event_tag_ids: Iterable[str],
        search_queries: Iterable[str],
    ) -> list[ProbeResult]:
        self.get("sports", "/sports")
        self.get("sports_market_types", "/sports/market-types")
        for tag_id in event_tag_ids:
            self.get(
                "soccer_events",
                "/events",
                {
                    "tag_id": tag_id,
                    "active": "true",
                    "closed": "false",
                    "limit": events_per_tag,
                },
            )
        first_token_id: str | None = None
        for query in search_queries:
            payload = self.get(
                "fixture_search",
                "/public-search",
                {
                    "q": query,
                    "events_status": "active",
                    "limit_per_type": 20,
                    "search_tags": "false",
                    "search_profiles": "false",
                },
            )
            if first_token_id is None:
                first_token_id = self._first_clob_token(payload)
        if first_token_id:
            self.get_clob("order_book", "/book", {"token_id": first_token_id})
            self.get_clob(
                "price_history",
                "/prices-history",
                {"market": first_token_id, "interval": "1d", "fidelity": 60},
            )
        return self.results

    @staticmethod
    def _first_clob_token(payload) -> str | None:
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            return None
        for event in payload["events"]:
            if not isinstance(event, dict):
                continue
            for market in event.get("markets", []):
                if not isinstance(market, dict) or not market.get("enableOrderBook"):
                    continue
                token_ids = market.get("clobTokenIds")
                if isinstance(token_ids, str):
                    try:
                        token_ids = json.loads(token_ids)
                    except json.JSONDecodeError:
                        token_ids = [token_ids]
                if isinstance(token_ids, list) and token_ids:
                    return str(token_ids[0])
        return None


class StatsBombProbe(BaseProbe):
    BASE_URL = "https://raw.githubusercontent.com/statsbomb/open-data/master"

    def __init__(self, client: HttpClient, store: RawArtifactStore) -> None:
        super().__init__(client, store, max_calls=4)

    def get(self, resource: str, path: str):
        self._check_budget()
        response = self.client.get(self.BASE_URL, path)
        return self._record("statsbomb_open", resource, response)

    def run(self, competition_name: str, season_name: str) -> list[ProbeResult]:
        competitions = self.get("competitions", "/data/competitions.json")
        selected = None
        if isinstance(competitions, list):
            candidates = [
                item
                for item in competitions
                if isinstance(item, dict)
                and item.get("competition_name") == competition_name
                and str(item.get("season_name")) == season_name
            ]
            if candidates:
                selected = candidates[0]
        if not selected:
            return self.results

        competition_id = selected["competition_id"]
        season_id = selected["season_id"]
        matches = self.get(
            "matches", f"/data/matches/{competition_id}/{season_id}.json"
        )
        if not isinstance(matches, list) or not matches:
            return self.results
        sample = self._choose_match(matches)
        match_id = sample.get("match_id")
        if not match_id:
            return self.results
        self.get("lineups", f"/data/lineups/{match_id}.json")
        self.get("events", f"/data/events/{match_id}.json")
        return self.results

    @staticmethod
    def _choose_match(matches: list[dict]) -> dict:
        for match in matches:
            teams = {
                match.get("home_team", {}).get("home_team_name"),
                match.get("away_team", {}).get("away_team_name"),
            }
            if teams == {"Argentina", "France"}:
                return match
        return matches[0]


class FootballDataUkProbe(BaseProbe):
    BASE_URL = "https://www.football-data.co.uk"

    def __init__(self, client: HttpClient, store: RawArtifactStore) -> None:
        super().__init__(client, store, max_calls=10)

    def run(self, csv_paths: Iterable[str]) -> list[ProbeResult]:
        for path in csv_paths:
            self._check_budget()
            response = self.client.get(self.BASE_URL, path)
            self.calls += 1
            artifact = self.store.store(
                source="football_data_uk",
                resource="league_csv",
                response=response,
                request_params={"path": path},
            )
            row_count = None
            note = ""
            if response.status == 200:
                try:
                    text = response.body.decode("utf-8-sig")
                    row_count = sum(1 for _ in csv.DictReader(io.StringIO(text)))
                except (UnicodeDecodeError, csv.Error) as error:
                    note = f"invalid_csv:{type(error).__name__}"
            self.results.append(
                ProbeResult(
                    "football_data_uk",
                    "league_csv",
                    response.status,
                    artifact,
                    row_count,
                    note,
                )
            )
        return self.results


class UnderstatProbe(BaseProbe):
    BASE_URL = "https://understat.com"

    def __init__(self, client: HttpClient, store: RawArtifactStore) -> None:
        super().__init__(client, store, max_calls=2)

    def run(self, league: str, season: str) -> list[ProbeResult]:
        self._check_budget()
        path = f"/getLeagueData/{league}/{season}"
        response = self.client.get(
            self.BASE_URL,
            path,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://understat.com/league/{league}/{season}",
            },
        )
        self._record(
            "understat",
            "league_data",
            response,
            {"league": league, "season": season},
        )
        return self.results
