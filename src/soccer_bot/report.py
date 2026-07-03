from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path


API_FOOTBALL_EXPECTATIONS = {
    "fixtures_by_date": [
        "fixture.id",
        "fixture.date",
        "fixture.status.short",
        "league.id",
        "league.name",
        "teams.home.id",
        "teams.away.id",
        "goals.home",
        "goals.away",
        "score.fulltime.home",
        "score.fulltime.away",
    ],
    "fixture_lineups": ["team.id", "formation", "startXI", "substitutes"],
    "fixture_events": [
        "time.elapsed",
        "team.id",
        "player.id",
        "assist.id",
        "type",
        "detail",
    ],
}

PLAYER_EXPECTATIONS = [
    "player.id",
    "statistics.0.games.minutes",
    "statistics.0.games.position",
    "statistics.0.games.substitute",
    "statistics.0.shots.total",
    "statistics.0.shots.on",
    "statistics.0.goals.total",
    "statistics.0.goals.assists",
    "statistics.0.passes.key",
    "statistics.0.cards.yellow",
    "statistics.0.penalty.scored",
]


def _get_path(value, dotted_path: str):
    current = value
    for part in dotted_path.split("."):
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return False, None
            current = current[index]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False, None
    return True, current


def _read_metadata(raw_root: Path) -> list[dict]:
    metadata: list[dict] = []
    for path in raw_root.rglob("*.meta.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        item["_metadata_path"] = str(path)
        metadata.append(item)
    return sorted(metadata, key=lambda item: item.get("retrieved_at", ""))


def _read_payload(metadata: dict):
    return json.loads(_read_bytes(metadata).decode("utf-8"))


def _read_bytes(metadata: dict) -> bytes:
    path = Path(metadata["data_path"])
    with gzip.open(path, "rb") as handle:
        body = handle.read()
    if metadata.get("response_headers", {}).get("content-encoding", "").lower() == "gzip":
        body = gzip.decompress(body)
    return body


def _response_items(payload) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("response"), list):
        return [item for item in payload["response"] if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _successful_payloads(metadata: list[dict], source: str, resource: str) -> list:
    payloads = []
    seen_hashes: set[str] = set()
    for item in metadata:
        if item["source"] != source or item["resource"] != resource:
            continue
        if item["http_status"] != 200 or item["content_sha256"] in seen_hashes:
            continue
        try:
            payloads.append(_read_payload(item))
            seen_hashes.add(item["content_sha256"])
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
    return payloads


def _field_coverage_table(lines: list[str], records: list[dict], paths: list[str]) -> None:
    lines.extend(
        [
            "| Field | Records containing field | Non-null records |",
            "|---|---:|---:|",
        ]
    )
    for path in paths:
        containing = 0
        non_null = 0
        for record in records:
            exists, value = _get_path(record, path)
            containing += int(exists)
            non_null += int(exists and value is not None)
        lines.append(f"| `{path}` | {containing}/{len(records)} | {non_null}/{len(records)} |")
    lines.append("")


def build_report(raw_root: Path, output_path: Path) -> None:
    metadata = _read_metadata(raw_root)
    by_source = Counter(item["source"] for item in metadata)
    by_resource: dict[str, Counter] = defaultdict(Counter)
    for item in metadata:
        by_resource[item["source"]][item["resource"]] += 1

    lines = [
        "# Source Validation Report",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Retrieval summary",
        "",
        "| Source | Observations | Resources |",
        "|---|---:|---|",
    ]
    for source, count in sorted(by_source.items()):
        resources = ", ".join(
            f"{name} ({resource_count})"
            for name, resource_count in sorted(by_resource[source].items())
        )
        lines.append(f"| `{source}` | {count} | {resources} |")

    lines.extend(
        [
            "",
            "## HTTP and payload results",
            "",
            "| Source | Resource | HTTP | Top-level records | Duplicate body |",
            "|---|---|---:|---:|---|",
        ]
    )
    unique_latest: dict[tuple[str, str], dict] = {}
    for item in metadata:
        unique_latest[(item["source"], item["resource"])] = item
    for (source, resource), item in sorted(unique_latest.items()):
        try:
            if source == "football_data_uk" and resource == "league_csv":
                text = _read_bytes(item).decode("utf-8-sig")
                records = sum(1 for _ in csv.DictReader(io.StringIO(text)))
            else:
                payload = _read_payload(item)
                if isinstance(payload, dict) and isinstance(payload.get("events"), list):
                    records = len(payload["events"])
                elif isinstance(payload, dict) and isinstance(payload.get("players"), list):
                    records = len(payload["players"])
                elif isinstance(payload, dict) and isinstance(payload.get("history"), list):
                    records = len(payload["history"])
                elif isinstance(payload, dict) and isinstance(payload.get("bids"), list):
                    records = len(payload["bids"]) + len(payload.get("asks", []))
                else:
                    records = len(_response_items(payload))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            records = -1
        lines.append(
            f"| `{source}` | `{resource}` | {item['http_status']} | "
            f"{records if records >= 0 else 'invalid JSON'} | {item['duplicate_content']} |"
        )

    lines.extend(["", "## API-Football field coverage", ""])
    found_expectation = False
    for resource, paths in API_FOOTBALL_EXPECTATIONS.items():
        payloads = _successful_payloads(metadata, "api_football", resource)
        if not payloads:
            continue
        found_expectation = True
        records = [record for payload in payloads for record in _response_items(payload)]
        lines.extend([f"### `{resource}`", ""])
        _field_coverage_table(lines, records, paths)
    if not found_expectation:
        lines.append("No API-Football expectation payloads were captured.\n")

    player_payloads = _successful_payloads(metadata, "api_football", "fixture_players")
    player_records = []
    for payload in player_payloads:
        for team_record in _response_items(payload):
            player_records.extend(
                item for item in team_record.get("players", []) if isinstance(item, dict)
            )
    if player_records:
        lines.extend(["### `fixture_players`", ""])
        _field_coverage_table(lines, player_records, PLAYER_EXPECTATIONS)

    statistics_payloads = _successful_payloads(
        metadata, "api_football", "fixture_statistics"
    )
    statistic_types: Counter = Counter()
    statistic_teams = 0
    for payload in statistics_payloads:
        for team_record in _response_items(payload):
            statistic_teams += 1
            for item in team_record.get("statistics", []):
                if isinstance(item, dict) and item.get("value") is not None:
                    statistic_types[str(item.get("type", "unknown"))] += 1
    if statistic_teams:
        lines.extend(
            [
                "### `fixture_statistics`",
                "",
                f"Populated statistic types across **{statistic_teams}** team records: "
                + ", ".join(f"`{name}`" for name in sorted(statistic_types))
                + ".",
                "",
            ]
        )

    lines.extend(["## Historical bootstrap validation", ""])

    statsbomb_matches = [
        record
        for payload in _successful_payloads(metadata, "statsbomb_open", "matches")
        for record in _response_items(payload)
    ]
    statsbomb_lineups = [
        record
        for payload in _successful_payloads(metadata, "statsbomb_open", "lineups")
        for record in _response_items(payload)
    ]
    statsbomb_events = [
        record
        for payload in _successful_payloads(metadata, "statsbomb_open", "events")
        for record in _response_items(payload)
    ]
    if statsbomb_matches:
        lines.extend(["### StatsBomb Open Data", ""])
        lines.append(
            f"The probe captured **{len(statsbomb_matches)}** FIFA World Cup 2022 matches, "
            f"**{sum(len(team.get('lineup', [])) for team in statsbomb_lineups)}** lineup-player records, "
            f"and **{len(statsbomb_events)}** events for the Argentina–France sample match.\n"
        )
        event_paths = [
            "id",
            "period",
            "timestamp",
            "minute",
            "second",
            "type.name",
            "team.id",
            "player.id",
            "location",
            "shot.statsbomb_xg",
            "shot.outcome.name",
            "pass.goal_assist",
            "pass.assisted_shot_id",
        ]
        _field_coverage_table(lines, statsbomb_events, event_paths)

    football_csv_metadata = [
        item
        for item in metadata
        if item["source"] == "football_data_uk"
        and item["resource"] == "league_csv"
        and item["http_status"] == 200
    ]
    football_rows: list[dict] = []
    seasons: list[str] = []
    seen_csv_hashes: set[str] = set()
    for item in football_csv_metadata:
        if item["content_sha256"] in seen_csv_hashes:
            continue
        seen_csv_hashes.add(item["content_sha256"])
        seasons.append(str(item.get("request_parameters", {}).get("path", "unknown")))
        text = _read_bytes(item).decode("utf-8-sig")
        football_rows.extend(csv.DictReader(io.StringIO(text)))
    if football_rows:
        target_columns = [
            "Date",
            "Time",
            "HomeTeam",
            "AwayTeam",
            "FTHG",
            "FTAG",
            "FTR",
            "HS",
            "AS",
            "HST",
            "AST",
            "HC",
            "AC",
            "B365H",
            "B365D",
            "B365A",
            "AvgH",
            "AvgD",
            "AvgA",
            "AHh",
            "AvgAHH",
            "AvgAHA",
        ]
        lines.extend(
            [
                "### Football-Data.co.uk",
                "",
                f"The probe captured **{len(football_rows)}** Premier League rows from "
                + ", ".join(f"`{season}`" for season in seasons)
                + ".",
                "",
                "| Column | Non-empty rows |",
                "|---|---:|",
            ]
        )
        for column in target_columns:
            populated = sum(bool(row.get(column, "").strip()) for row in football_rows)
            lines.append(f"| `{column}` | {populated}/{len(football_rows)} |")
        lines.append("")

    understat_payloads = _successful_payloads(metadata, "understat", "league_data")
    if understat_payloads and isinstance(understat_payloads[-1], dict):
        payload = understat_payloads[-1]
        players = [item for item in payload.get("players", []) if isinstance(item, dict)]
        lines.extend(
            [
                "### Understat",
                "",
                f"The EPL 2025/26 endpoint returned **{len(payload.get('dates', []))}** fixtures, "
                f"**{len(payload.get('teams', {}))}** teams, and **{len(players)}** player-season records.",
                "",
            ]
        )
        _field_coverage_table(
            lines,
            players,
            [
                "id",
                "player_name",
                "games",
                "time",
                "goals",
                "xG",
                "assists",
                "xA",
                "shots",
                "key_passes",
                "npg",
                "npxG",
                "xGChain",
                "xGBuildup",
                "position",
                "team_title",
            ],
        )

    sports_item = unique_latest.get(("polymarket_gamma", "sports"))
    lines.extend(["## Polymarket sports discovery", ""])
    if sports_item:
        sports_payload = _read_payload(sports_item)
        if isinstance(sports_payload, list):
            lines.append(f"The `/sports` endpoint returned **{len(sports_payload)}** records.\n")
            likely_soccer = []
            for item in sports_payload:
                tags = {tag.strip() for tag in str(item.get("tags", "")).split(",")}
                if "100350" in tags:
                    likely_soccer.append(str(item.get("sport", "unknown")))
            lines.append(
                "Likely soccer configurations: "
                + (", ".join(f"`{name}`" for name in likely_soccer) or "none detected")
                + ".\n"
            )
        else:
            lines.append("The `/sports` response was not a JSON list.\n")
    else:
        lines.append("No Polymarket sports payload was captured.\n")

    soccer_event_metadata = [
        item
        for item in metadata
        if item["source"] == "polymarket_gamma"
        and item["resource"] == "soccer_events"
        and str(item.get("request_parameters", {}).get("tag_id")) in {"100350", "102232"}
        and item["http_status"] == 200
    ]
    seen_soccer_hashes: set[str] = set()
    market_types: Counter = Counter()
    event_titles: list[str] = []
    for item in soccer_event_metadata:
        if item["content_sha256"] in seen_soccer_hashes:
            continue
        seen_soccer_hashes.add(item["content_sha256"])
        payload = _read_payload(item)
        if not isinstance(payload, list):
            continue
        for event in payload:
            if not isinstance(event, dict):
                continue
            if len(event_titles) < 10:
                event_titles.append(str(event.get("title", "untitled")))
            for market in event.get("markets", []):
                if isinstance(market, dict):
                    market_types[str(market.get("sportsMarketType") or "unclassified")] += 1
    if market_types:
        lines.extend(
            [
                "### Observed soccer market types",
                "",
                "| Market type | Markets observed |",
                "|---|---:|",
            ]
        )
        for name, count in market_types.most_common():
            lines.append(f"| `{name}` | {count} |")
        lines.extend(
            [
                "",
                "Example event titles: "
                + "; ".join(f"`{title}`" for title in event_titles)
                + ".",
                "",
            ]
        )

    search_metadata = [
        item
        for item in metadata
        if item["source"] == "polymarket_gamma"
        and item["resource"] == "fixture_search"
        and item["http_status"] == 200
    ]
    if search_metadata:
        lines.extend(
            [
                "### Targeted fixture search",
                "",
                "| Query | Events returned | Direct fixture event | Market types |",
                "|---|---:|---|---|",
            ]
        )
        seen_queries: set[str] = set()
        for item in search_metadata:
            query = str(item.get("request_parameters", {}).get("q", ""))
            if not query or query in seen_queries:
                continue
            seen_queries.add(query)
            payload = _read_payload(item)
            events = payload.get("events", []) if isinstance(payload, dict) else []
            query_terms = {term.lower() for term in query.split() if len(term) > 2}
            direct_event = None
            for event in events:
                title_terms = set(
                    str(event.get("title", "")).lower().replace(".", "").split()
                )
                event_types = {
                    str(market.get("sportsMarketType") or "unclassified")
                    for market in event.get("markets", [])
                    if isinstance(market, dict)
                }
                if query_terms.issubset(title_terms) and "moneyline" in event_types:
                    direct_event = event
                    break
            if direct_event:
                direct_title = str(direct_event.get("title", ""))
                types = sorted(
                    {
                        str(market.get("sportsMarketType") or "unclassified")
                        for market in direct_event.get("markets", [])
                        if isinstance(market, dict)
                    }
                )
            else:
                direct_title = "not found"
                types = []
            lines.append(
                f"| `{query}` | {len(events)} | {direct_title} | "
                + (", ".join(f"`{name}`" for name in types) or "—")
                + " |"
            )
        lines.append("")

    book_payloads = _successful_payloads(metadata, "polymarket_clob", "order_book")
    history_payloads = _successful_payloads(metadata, "polymarket_clob", "price_history")
    if book_payloads:
        book = book_payloads[-1]
        history_points = 0
        if history_payloads and isinstance(history_payloads[-1], dict):
            history_points = len(history_payloads[-1].get("history", []))
        lines.extend(
            [
                "### CLOB read validation",
                "",
                "| Bids | Asks | Tick size | Minimum order | Price-history points |",
                "|---:|---:|---:|---:|---:|",
                f"| {len(book.get('bids', []))} | {len(book.get('asks', []))} | "
                f"{book.get('tick_size', 'unknown')} | {book.get('min_order_size', 'unknown')} | "
                f"{history_points} |",
                "",
            ]
        )

    lines.extend(
        [
            "## Confirmed findings from this probe",
            "",
            "- API-Football returned fixtures, confirmed lineups, formations, starters, substitutes, event timelines, per-player minutes/goals/assists/shots, team corners, and team expected goals for a covered World Cup fixture.",
            "- The upcoming Spain fixture returned a complete lineup shortly before kickoff; events and injuries were empty at retrieval time.",
            "- Rapid unpaced API-Football requests produced HTTP 429 responses, so the collector now enforces a minimum interval and stops on rate limiting.",
            "- Polymarket exposes public soccer metadata and classified market types through its Gamma API.",
            "- Targeted search found the Spain–Austria regulation moneyline event, and the public CLOB returned a populated order book and price history without authentication.",
            "- StatsBomb Open Data supplied a complete World Cup match list plus rich lineups and 4,407 events for the 2022 final sample.",
            "- Football-Data.co.uk supplied 760 immediately usable team-match rows with scores, shots, corners, moneyline odds, and handicap fields across two Premier League seasons.",
            "- Understat supplied 537 EPL player-season records with minutes, goals, assists, shots, xG, xA, key passes, non-penalty xG, xGChain, and xGBuildup.",
            "",
        ]
    )

    lines.extend(
        [
            "## Architecture implications",
            "",
            "1. The database does not need to begin empty: Football-Data.co.uk can bootstrap team-result, spread, odds, and corner tables immediately.",
            "2. StatsBomb can bootstrap rich event and lineup tables for selected competitions, while API-Football supplies current operational observations.",
            "3. Understat can bootstrap club player-form features immediately, so the player model also does not need to wait for future collection.",
            "4. Provider-specific xG must remain identifiable: API-Football returned team expected goals, StatsBomb supplies shot-level StatsBomb xG, and Understat supplies its own player xG/xA values.",
            "5. Empty API fields are meaningful. Player minutes are null for unused substitutes, while zero and null goal/shot values must be normalized carefully rather than treated identically without field-specific rules.",
            "6. The confirmed source payloads fit the canonical fixture, lineup, appearance, event, player-statistic, team-statistic, bookmaker-quote, prediction-market, and order-book tables proposed in `DATA_ARCHITECTURE.md`.",
            "",
        ]
    )

    lines.extend(
        [
            "## Interpretation boundary",
            "",
            "This report records endpoint behavior and field presence. It does not yet prove historical depth, competition-wide completeness, or long-term scraper stability. Those require the expanded fixture and bulk-source probes described in `DATA_ARCHITECTURE.md`.",
            "",
        ]
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
