"""Phase 5: copy-trading leader scoring parity — ports src/copy/leaderScoring.ts."""
from __future__ import annotations

from bybit_agent.copy.leader_scoring import score_leaders


def _leader(roi=0.2, max_dd=0.1, sharpe=1.5, leader_mark="L1", nickname="Alice"):
    return {
        "leaderMark": leader_mark,
        "nickName": nickname,
        "roi": str(roi),
        "maxDrawdown": str(max_dd),
        "sharpeRatio": str(sharpe),
    }


def test_score_formula():
    # score = roi*0.4 + sharpe*0.4 - maxDD*0.2
    l = score_leaders([_leader(roi=0.2, max_dd=0.1, sharpe=1.5)])[0]
    expected = 0.2 * 0.4 + 1.5 * 0.4 - 0.1 * 0.2
    assert abs(l.score - expected) < 1e-9


def test_filters_high_drawdown():
    leaders = [
        _leader(max_dd=0.29, leader_mark="OK"),
        _leader(max_dd=0.30, leader_mark="EDGE"),   # exactly 0.30 — filtered (< not <=)
        _leader(max_dd=0.50, leader_mark="BAD"),
    ]
    scored = score_leaders(leaders)
    marks = [l.leader_mark for l in scored]
    assert "OK" in marks
    assert "EDGE" not in marks
    assert "BAD" not in marks


def test_sorted_descending_by_score():
    leaders = [
        _leader(roi=0.1, sharpe=0.5, max_dd=0.05, leader_mark="Low"),
        _leader(roi=0.5, sharpe=2.0, max_dd=0.05, leader_mark="High"),
        _leader(roi=0.3, sharpe=1.0, max_dd=0.05, leader_mark="Mid"),
    ]
    scored = score_leaders(leaders)
    scores = [l.score for l in scored]
    assert scores == sorted(scores, reverse=True)
    assert scored[0].leader_mark == "High"


def test_empty_input():
    assert score_leaders([]) == []


def test_string_numeric_fields_parsed():
    # Fields come from the API as strings.
    l = score_leaders([_leader(roi="0.3", max_dd="0.05", sharpe="1.2")])[0]
    assert isinstance(l.roi, float)
    assert isinstance(l.max_drawdown, float)
    assert isinstance(l.sharpe, float)
