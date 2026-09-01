from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .collector import fetch_current
from .parser import PLAY_PAGES, parse_page


def run_probe() -> dict[str, object]:
    results: list[dict[str, object]] = []
    for market in PLAY_PAGES:
        started = time.monotonic()
        try:
            raw = fetch_current(market, timeout=20)
            events, prices = parse_page(raw, market_type=market)
            if not events:
                raise ValueError("page parsed but contained no events")
            results.append({
                "market": market,
                "ok": True,
                "bytes": len(raw),
                "events": len(events),
                "prices": len(prices),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            })
        except Exception as exc:
            results.append({
                "market": market,
                "ok": False,
                "error_class": type(exc).__name__,
                "error": str(exc)[:300],
                "elapsed_seconds": round(time.monotonic() - started, 3),
            })
    passed = sum(bool(item["ok"]) for item in results)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "passed_markets": passed,
        "total_markets": len(results),
        "all_passed": passed == len(results),
        "results": results,
    }


def main() -> None:
    report = run_probe()
    Path("connectivity_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["all_passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
