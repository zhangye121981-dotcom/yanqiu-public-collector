from __future__ import annotations

import re


LISTED_CORRECT_SCORES = {
    "1:0","2:0","2:1","3:0","3:1","3:2","4:0","4:1","4:2",
    "0:0","1:1","2:2","3:3",
    "0:1","0:2","1:2","0:3","1:3","2:3","0:4","1:4","2:4",
}


def correct_score_selection(score: str) -> str:
    normalized = re.sub(r"\s+", "", score).replace("：", ":")
    if normalized in LISTED_CORRECT_SCORES: return normalized
    home, away = (int(x) for x in normalized.split(":"))
    return ("胜" if home > away else "平" if home == away else "负") + "其他"
