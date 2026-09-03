from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from yanqiu_public.collector import (
    RESERVED_CLOUD_ID_FLOOR,
    capture_cycle,
    ensure_schema,
    ingest_market,
)
from yanqiu_public.parser import issue_numbers, parse_page


HTML = b'''<html><head><meta charset="utf-8"></head><body>
<select id="expect_select"><option value="26089" selected>26089</option></select>
<table id="vs_table"><tbody>
<tr class="vs_lines" fid="42" value="{index:'7',leagueName:'\xe8\x8b\xb1\xe8\xb6\x85',homeTeam:'\xe4\xb8\xbb\xe9\x98\x9f',guestTeam:'\xe5\xae\xa2\xe9\x98\x9f',endTime:'2026-09-02 18:20',disabled:'no'}">
<td>7</td><td>\xe8\x8b\xb1\xe8\xb6\x85</td><td>18:20</td><td>\xe4\xb8\xbb\xe9\x98\x9f</td><td>-1</td><td>\xe5\xae\xa2\xe9\x98\x9f</td><td>-</td><td>-</td><td>-</td><td>SP</td></tr>
<tr class="hide_b"><td>
<label>1:0 (7.25)</label><label>2:0 (30.00)</label><label>2:1 (30.00)</label>
<label>3:0 (30.00)</label><label>3:1 (30.00)</label><label>3:2 (30.00)</label>
<label>4:0 (30.00)</label><label>4:1 (30.00)</label><label>4:2 (30.00)</label>
<label>0:0 (30.00)</label><label>1:1 (30.00)</label><label>2:2 (30.00)</label><label>3:3 (30.00)</label>
<label>0:1 (30.00)</label><label>0:2 (30.00)</label><label>1:2 (30.00)</label>
<label>0:3 (30.00)</label><label>1:3 (30.00)</label><label>2:3 (30.00)</label>
<label>0:4 (30.00)</label><label>1:4 (30.00)</label><label>2:4 (30.00)</label>
<label>\xe8\x83\x9c\xe5\x85\xb6\xe4\xbb\x96 (30.00)</label><label>\xe5\xb9\xb3\xe5\x85\xb6\xe4\xbb\x96 (30.00)</label><label>\xe8\xb4\x9f\xe5\x85\xb6\xe4\xbb\x96 (30.00)</label>
</td></tr></tbody></table></body></html>'''

CLOSED_HTML = HTML.replace(b"disabled:'no'", b"disabled:'yes'").replace(
    b"<td>-</td><td>-</td><td>-</td><td>SP</td>",
    b"<td>-</td><td>2:1</td><td>7.25</td><td>SP</td>",
)


class CollectorTests(unittest.TestCase):
    def test_parser_surface_is_preserved(self):
        self.assertEqual(issue_numbers(HTML), ["26089"])
        events, prices = parse_page(HTML, market_type="correct_score")
        self.assertEqual(events[0].provider_match_id, "42")
        self.assertTrue(events[0].selling)
        self.assertEqual(events[0].provider_event_time_text, "18:20")
        self.assertEqual(len(prices), 25)

    def test_cloud_ids_are_reserved_and_rows_are_append_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cloud.sqlite"
            con = sqlite3.connect(path)
            ensure_schema(con)
            sid = ingest_market(
                con,
                HTML,
                market="correct_score",
                captured_at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
            )
            self.assertGreaterEqual(sid, RESERVED_CLOUD_ID_FLOOR)
            self.assertEqual(con.execute("SELECT count(*) FROM source500_events").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM source500_prices").fetchone()[0], 25)
            self.assertEqual(con.execute("SELECT count(*) FROM forward_public_quotes").fetchone()[0], 25)
            columns = {row[1] for row in con.execute("PRAGMA table_info(source500_events)")}
            self.assertIn("provider_event_time_text", columns)
            con.execute(
                "INSERT INTO cloud_collection_runs VALUES(NULL,?,?,?,?,?)",
                ("2026-09-01T10:00:00+00:00", 1, 0, "[]", "x"),
            )
            con.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE cloud_collection_runs SET captured_markets=0")
            con.close()

    def test_result_availability_is_first_observed_time_and_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.sqlite"
            con = sqlite3.connect(path)
            ensure_schema(con)
            first = datetime(2026, 9, 2, 20, tzinfo=timezone.utc)
            later = datetime(2026, 9, 2, 21, tzinfo=timezone.utc)
            ingest_market(con, CLOSED_HTML, market="correct_score", captured_at=first)
            ingest_market(con, CLOSED_HTML, market="correct_score", captured_at=later)
            rows = con.execute(
                "SELECT final_score,observed_at FROM source500_result_observations"
            ).fetchall()
            self.assertEqual(rows, [("2:1", first.isoformat())])
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute(
                    "UPDATE source500_result_observations SET observed_at=?",
                    (later.isoformat(),),
                )
            con.close()

    def test_cycle_records_partial_failure_without_discarding_success(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cycle.sqlite"

            def connect_fn():
                return sqlite3.connect(path)

            def fetcher(market: str) -> bytes:
                if market == "correct_score":
                    return HTML
                raise TimeoutError("simulated source timeout")

            result = capture_cycle(
                connect_fn=connect_fn,
                fetcher=fetcher,
                now=datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
            )
            self.assertEqual(result["status"], "degraded")
            self.assertEqual(result["captured_markets"], 1)
            self.assertEqual(result["failed_markets"], 4)
            with sqlite3.connect(path) as con:
                self.assertEqual(con.execute("SELECT count(*) FROM source500_snapshots").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT count(*) FROM forward_public_cycles").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
