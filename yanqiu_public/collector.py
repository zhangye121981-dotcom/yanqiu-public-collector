from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .parser import PLAY_PAGES, parse_page


BASE_URL = "https://trade.500.com"
RESERVED_CLOUD_ID_FLOOR = 10_000_000


def _log(message: str) -> None:
    print(f"[collector] {message}", flush=True)


SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS source500_snapshots(
      snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
      issue_num TEXT NOT NULL,
      market_type TEXT NOT NULL,
      captured_at TEXT NOT NULL,
      content_sha256 TEXT NOT NULL,
      html TEXT NOT NULL,
      UNIQUE(issue_num,market_type,captured_at,content_sha256)
    )""",
    """CREATE TABLE IF NOT EXISTS source500_events(
      snapshot_id INTEGER NOT NULL REFERENCES source500_snapshots(snapshot_id),
      provider_match_id TEXT NOT NULL,
      issue_num TEXT NOT NULL,
      play_num INTEGER NOT NULL,
      competition TEXT,
      home_team TEXT,
      away_team TEXT,
      handicap REAL,
      close_time TEXT NOT NULL,
      selling INTEGER NOT NULL,
      final_score TEXT,
      PRIMARY KEY(snapshot_id,provider_match_id)
    )""",
    """CREATE TABLE IF NOT EXISTS source500_prices(
      snapshot_id INTEGER NOT NULL REFERENCES source500_snapshots(snapshot_id),
      provider_match_id TEXT NOT NULL,
      issue_num TEXT NOT NULL,
      play_num INTEGER NOT NULL,
      market_type TEXT NOT NULL,
      selection TEXT NOT NULL,
      sp REAL NOT NULL,
      price_kind TEXT NOT NULL,
      PRIMARY KEY(snapshot_id,provider_match_id,market_type,selection,price_kind)
    )""",
    """CREATE TABLE IF NOT EXISTS forward_public_quotes(
      source_name TEXT NOT NULL,
      source_snapshot_id INTEGER NOT NULL,
      provider_odds_id TEXT NOT NULL,
      event_id TEXT NOT NULL,
      issue_num TEXT NOT NULL,
      play_num INTEGER NOT NULL,
      market_type TEXT NOT NULL,
      selection TEXT NOT NULL,
      line REAL,
      decimal_odds REAL NOT NULL CHECK(decimal_odds>1),
      captured_at TEXT NOT NULL,
      close_time TEXT NOT NULL,
      PRIMARY KEY(source_name,source_snapshot_id,provider_odds_id,market_type,selection)
    )""",
    """CREATE TABLE IF NOT EXISTS forward_public_cycles(
      cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
      captured_at TEXT NOT NULL,
      captured_markets INTEGER NOT NULL,
      failed_markets INTEGER NOT NULL,
      next_interval_seconds INTEGER NOT NULL,
      errors_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS cloud_collection_runs(
      run_id INTEGER PRIMARY KEY AUTOINCREMENT,
      captured_at TEXT NOT NULL,
      captured_markets INTEGER NOT NULL,
      failed_markets INTEGER NOT NULL,
      snapshot_ids_json TEXT NOT NULL,
      evidence_sha256 TEXT NOT NULL UNIQUE
    )""",
    "CREATE INDEX IF NOT EXISTS ix_source500_event ON source500_events(issue_num,play_num)",
    "CREATE INDEX IF NOT EXISTS ix_source500_event_latest ON source500_events(issue_num,play_num,snapshot_id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_public_quotes_latest ON forward_public_quotes(provider_odds_id,market_type,captured_at DESC)",
    """CREATE TRIGGER IF NOT EXISTS cloud_runs_no_update BEFORE UPDATE ON cloud_collection_runs
       BEGIN SELECT RAISE(ABORT,'cloud collection runs are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS cloud_runs_no_delete BEFORE DELETE ON cloud_collection_runs
       BEGIN SELECT RAISE(ABORT,'cloud collection runs are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS cloud_cycles_no_update BEFORE UPDATE ON forward_public_cycles
       BEGIN SELECT RAISE(ABORT,'public cycles are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS cloud_cycles_no_delete BEFORE DELETE ON forward_public_cycles
       BEGIN SELECT RAISE(ABORT,'public cycles are immutable'); END""",
)


def _sha(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return value.astimezone(timezone.utc).isoformat()


def _insert_rows_batched(
    con,
    insert_prefix: str,
    rows: list[tuple[object, ...]],
    *,
    columns: int,
    batch_size: int,
) -> None:
    """Insert many rows with a small number of remote database requests."""
    row_placeholder = "(" + ",".join("?" for _ in range(columns)) + ")"
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset:offset + batch_size]
        placeholders = ",".join(row_placeholder for _ in batch)
        parameters = tuple(value for row in batch for value in row)
        con.execute(f"{insert_prefix} VALUES {placeholders}", parameters)


def connect_remote():
    url = os.environ.get("TURSO_DATABASE_URL", "").strip()
    token = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        raise RuntimeError("TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be configured as GitHub secrets")
    import libsql

    return libsql.connect(database=url, auth_token=token)


def ensure_schema(con) -> None:
    expected_objects = 13
    _log("checking remote schema")
    ready = con.execute(
        """SELECT count(*) FROM sqlite_master WHERE name IN (
        'source500_snapshots','source500_events','source500_prices',
        'forward_public_quotes','forward_public_cycles','cloud_collection_runs',
        'ix_source500_event','ix_source500_event_latest','ix_public_quotes_latest',
        'cloud_runs_no_update','cloud_runs_no_delete',
        'cloud_cycles_no_update','cloud_cycles_no_delete')"""
    ).fetchone()
    if ready is not None and int(ready[0]) == expected_objects:
        _log("remote schema already ready; skipping DDL")
        return
    for index, statement in enumerate(SCHEMA_STATEMENTS, start=1):
        _log(f"applying schema object {index}/{len(SCHEMA_STATEMENTS)}")
        con.execute(statement)
    for table in ("source500_snapshots", "forward_public_cycles", "cloud_collection_runs"):
        row = con.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        if row is None:
            con.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, RESERVED_CLOUD_ID_FLOOR - 1))
        elif int(row[0]) < RESERVED_CLOUD_ID_FLOOR - 1:
            con.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (RESERVED_CLOUD_ID_FLOOR - 1, table))
    con.commit()
    _log("remote schema ready")


def fetch_current(market: str, timeout: float = 25.0, attempts: int = 3) -> bytes:
    request = Request(
        BASE_URL + PLAY_PAGES[market],
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; YanQiuResearch/2.0; public-source-audit)",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except (URLError, TimeoutError, OSError) as exc:
            if attempt >= attempts:
                raise
            delay = attempt * 2
            _log(
                f"fetch retry: {market} attempt={attempt + 1}/{attempts} "
                f"after {type(exc).__name__}; waiting {delay}s"
            )
            time.sleep(delay)
    raise RuntimeError(f"{market} fetch retry loop ended unexpectedly")


def adaptive_interval_seconds(source: str | bytes, market: str, now: datetime) -> int:
    events, _ = parse_page(source, market_type=market)
    local_now = now.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    remaining = [
        (event.close_time - local_now).total_seconds()
        for event in events
        if event.selling and event.close_time > local_now
    ]
    if not remaining:
        return 1800
    nearest = min(remaining)
    return 60 if nearest <= 3600 else 300 if nearest <= 21600 else 900


def ingest_market(con, raw: bytes, *, market: str, captured_at: datetime) -> int:
    events, prices = parse_page(raw, market_type=market)
    if not events:
        raise ValueError(f"{market} page contains no events")
    issue = events[0].issue_num
    if {event.issue_num for event in events} != {issue}:
        raise ValueError(f"{market} page contains mixed issues")
    captured = captured_at.astimezone(timezone.utc).isoformat()
    digest = hashlib.sha256(raw).hexdigest()

    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(
            """INSERT OR IGNORE INTO source500_snapshots
               (issue_num,market_type,captured_at,content_sha256,html) VALUES(?,?,?,?,?)""",
            (issue, market, captured, digest, raw),
        )
        row = con.execute(
            """SELECT snapshot_id FROM source500_snapshots
               WHERE issue_num=? AND market_type=? AND captured_at=? AND content_sha256=?""",
            (issue, market, captured, digest),
        ).fetchone()
        if row is None:
            raise RuntimeError("snapshot insert could not be verified")
        snapshot_id = int(row[0])
        existing = int(con.execute(
            "SELECT count(*) FROM source500_events WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()[0])
        if existing == 0:
            event_rows = [
                    (
                        snapshot_id,
                        event.provider_match_id,
                        event.issue_num,
                        event.play_num,
                        event.competition,
                        event.home_team,
                        event.away_team,
                        event.handicap,
                        _utc_iso(event.close_time),
                        int(event.selling),
                        event.final_score,
                    )
                    for event in events
                ]
            price_rows = [
                    (
                        snapshot_id,
                        price.provider_match_id,
                        price.issue_num,
                        price.play_num,
                        price.market_type,
                        price.selection,
                        price.sp,
                        price.price_kind,
                    )
                    for price in prices
                ]
            _insert_rows_batched(
                con,
                "INSERT INTO source500_events",
                event_rows,
                columns=11,
                batch_size=50,
            )
            _insert_rows_batched(
                con,
                "INSERT INTO source500_prices",
                price_rows,
                columns=8,
                batch_size=100,
            )
            con.execute(
                """INSERT OR IGNORE INTO forward_public_quotes
                   SELECT '500wan',p.snapshot_id,'500:'||p.issue_num||':'||p.play_num,
                          p.issue_num||':'||p.play_num,p.issue_num,p.play_num,p.market_type,
                          p.selection,e.handicap,p.sp,s.captured_at,e.close_time
                   FROM source500_prices p JOIN source500_snapshots s USING(snapshot_id)
                   JOIN source500_events e ON e.snapshot_id=p.snapshot_id
                     AND e.provider_match_id=p.provider_match_id
                   WHERE p.snapshot_id=? AND p.price_kind='live_reference_sp'
                     AND e.selling=1 AND s.captured_at<e.close_time""",
                (snapshot_id,),
            )
        con.commit()
        return snapshot_id
    except Exception:
        con.rollback()
        raise


def capture_cycle(*, connect_fn=connect_remote, fetcher=fetch_current,
                  now: datetime | None = None) -> dict[str, object]:
    captured_at = now or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    _log("connecting to Turso")
    con = connect_fn()
    _log("connected to Turso")
    try:
        ensure_schema(con)
        completed: list[tuple[str, int]] = []
        errors: list[tuple[str, str, str]] = []
        intervals: list[int] = []
        for market in PLAY_PAGES:
            try:
                _log(f"fetch start: {market}")
                raw = fetcher(market)
                _log(f"fetch complete: {market} ({len(raw)} bytes)")
                intervals.append(adaptive_interval_seconds(raw, market, captured_at))
                _log(f"ingest start: {market}")
                completed.append((market, ingest_market(con, raw, market=market, captured_at=captured_at)))
                _log(f"ingest complete: {market}")
            except Exception as exc:
                _log(f"market failed: {market}: {type(exc).__name__}: {str(exc)[:200]}")
                errors.append((market, type(exc).__name__, str(exc)[:300]))
        interval = min(intervals) if intervals else 1800
        evidence = _sha((captured_at.isoformat(), completed, errors, interval))
        con.execute(
            "INSERT OR IGNORE INTO forward_public_cycles VALUES(NULL,?,?,?,?,?)",
            (captured_at.isoformat(), len(completed), len(errors), interval,
             json.dumps(errors, ensure_ascii=False)),
        )
        con.execute(
            "INSERT OR IGNORE INTO cloud_collection_runs VALUES(NULL,?,?,?,?,?)",
            (captured_at.isoformat(), len(completed), len(errors),
             json.dumps(completed, ensure_ascii=False), evidence),
        )
        con.commit()
        _log(f"cycle committed: captured={len(completed)} failed={len(errors)}")
        return {
            "status": "completed" if not errors else "degraded",
            "captured_markets": len(completed),
            "failed_markets": len(errors),
            "snapshot_ids": dict(completed),
            "errors": errors,
            "next_interval_seconds": interval,
            "evidence_sha256": evidence,
        }
    finally:
        con.close()


def main() -> None:
    result = capture_cycle()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if int(result["captured_markets"]) == 0:
        raise SystemExit(2)
    if int(result["failed_markets"]) > 0:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
