from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Iterable

from lxml import html

from .bjdc_rules import correct_score_selection


PLAY_PAGES = {
    "rqspf": "/bjdc/index.php",
    "total_goals": "/bjdc/project_fq_jq.php",
    "upper_lower_odd_even": "/bjdc/project_fq_ds.php",
    "correct_score": "/bjdc/project_fq_bf.php",
    "half_full": "/bjdc/project_fq_bq.php",
}


@dataclass(frozen=True)
class FiveHundredEvent:
    issue_num: str
    play_num: int
    provider_match_id: str
    competition: str
    home_team: str
    away_team: str
    handicap: float
    close_time: datetime
    selling: bool
    final_score: str | None


@dataclass(frozen=True)
class FiveHundredPrice:
    provider_match_id: str
    issue_num: str
    play_num: int
    market_type: str
    selection: str
    sp: float
    price_kind: str  # live_reference_sp or official_result_sp


def _document(source: str | bytes):
    if isinstance(source, bytes):
        match = re.search(br"charset\s*=\s*['\"]?([^'\"\s/>;]+)",source[:8192],re.I)
        charset = match.group(1).decode("ascii",errors="ignore").lower() if match else "utf-8"
        if charset in {"gb2312","gbk","gb18030"}: charset = "gb18030"
        source = source.decode(charset, errors="replace")
    # Cloud-browser exports can prepend an accessibility overlay before the real head.
    head = source.find("<head")
    if head > 0:
        source = "<html>" + source[head:]
    return html.fromstring(source)


def issue_numbers(source: str | bytes) -> list[str]:
    doc = _document(source)
    return [x.get("value") for x in doc.xpath('//select[@id="expect_select"]/option') if x.get("value")]


def _meta(value: str) -> dict[str, str]:
    return {key: val.strip("'\"") for key, val in re.findall(
        r"(\w+)\s*:\s*('[^']*'|\"[^\"]*\"|[^,}]+)", value or ""
    )}


def _label_prices(row) -> Iterable[tuple[str, float]]:
    for label in row.xpath('.//label'):
        text = " ".join(label.text_content().split())
        match = re.match(r"(.+?)\s*\((\d+(?:\.\d+)?)\)$", text)
        if match:
            yield match.group(1).strip(), float(match.group(2))


def _grid_live_prices(row, market_type: str) -> Iterable[tuple[str, float]]:
    """Read in-row selectable SP values used by the four non-score live grids."""
    for label in row.xpath('.//label[.//input[@value]]'):
        value = label.xpath('.//input[@value]/@value')
        if not value:
            continue
        selection = value[0].replace('+', '').replace('-', '')
        sp_text = label.xpath('string(.//*[contains(concat(" ",normalize-space(@class)," ")," sp_value ")])').strip()
        try:
            sp = float(sp_text)
        except ValueError:
            continue
        if sp > 0:
            yield selection, sp


_GRID_LAYOUTS = {
    "rqspf": (7, 8, ("胜", "平", "负")),
    "total_goals": (5, 6, ("0", "1", "2", "3", "4", "5", "6", "7+")),
    "upper_lower_odd_even": (5, 6, ("上单", "上双", "下单", "下双")),
    "half_full": (5, 6, ("胜胜", "胜平", "胜负", "平胜", "平平", "平负", "负胜", "负平", "负负")),
}


def parse_page(source: str | bytes, *, market_type: str) -> tuple[list[FiveHundredEvent], list[FiveHundredPrice]]:
    if market_type not in PLAY_PAGES:
        raise ValueError(f"unsupported 500 market {market_type}")
    doc = _document(source)
    selected = doc.xpath('//select[@id="expect_select"]/option[@selected]/@value')
    if not selected:
        raise ValueError("missing selected BJDC issue")
    issue = selected[0]
    events, prices = [], []
    rows = doc.xpath('//table[@id="vs_table"]//tr[contains(concat(" ",normalize-space(@class)," ")," vs_lines ")]')
    for row in rows:
        cells = [" ".join(x.text_content().split()) for x in row.xpath('./td')]
        if len(cells) < 9:
            continue
        meta = _meta(row.get("value", ""))
        play_num = int(meta.get("index", cells[0]))
        close = datetime.fromisoformat(meta["endTime"])
        selling = meta.get("disabled") == "no"
        score_index = 7 if market_type == "correct_score" else _GRID_LAYOUTS[market_type][0]
        final_score = cells[score_index] if len(cells) > score_index and re.fullmatch(r"\d+\s*:\s*\d+", cells[score_index]) else None
        handicap_text = meta.get("rangqiuNum")
        if handicap_text is None:
            handicap_text = cells[4] if market_type in {"rqspf", "correct_score"} else "0"
        event = FiveHundredEvent(
            issue, play_num, row.get("fid", ""), meta.get("leagueName", cells[1]),
            meta.get("homeTeam", cells[3]), meta.get("guestTeam", cells[5] if market_type in {"rqspf","correct_score"} else cells[4]),
            float(str(handicap_text).replace("+", "")), close, selling, final_score,
        )
        events.append(event)
        if selling and market_type != "correct_score":
            for selection, sp in _grid_live_prices(row, market_type):
                prices.append(FiveHundredPrice(event.provider_match_id, issue, play_num,
                                               market_type, selection, sp,
                                               "live_reference_sp"))
        if not selling and final_score:
            candidates = []
            if market_type == "correct_score":
                candidates = [(correct_score_selection(final_score), cells[8] if len(cells) > 8 else "--")]
            else:
                _, start, labels = _GRID_LAYOUTS[market_type]
                candidates = list(zip(labels, cells[start:start + len(labels)]))
            positive = []
            for selection, value in candidates:
                try: result_sp = float(value)
                except ValueError: continue
                if result_sp > 0: positive.append((selection,result_sp))
            # A result row must identify exactly one winning option. Some closed
            # archive pages retain all reference values; those are not timestamped
            # historical quotes and must never be relabelled as result SP.
            if len(positive) == 1:
                selection, result_sp = positive[0]
                prices.append(FiveHundredPrice(event.provider_match_id, issue, play_num,
                                               market_type, selection, result_sp,
                                               "official_result_sp"))
        detail = row.getnext()
        if detail is None or "hide_b" not in (detail.get("class") or ""):
            continue
        # Closed pages expose the authoritative winning SP in the main row. The
        # detail grid often renders all other values as "--", so never infer a
        # historical quote surface from it.
        if not selling:
            continue
        kind = "live_reference_sp"
        for selection, sp in _label_prices(detail):
            if sp <= 0: continue
            prices.append(FiveHundredPrice(event.provider_match_id, issue, play_num,
                                             market_type, selection, sp, kind))
    return events, prices
