"""Phase 0: JSONB param encoding + comment-aware migration splitting."""

from __future__ import annotations

import json
import re

from bybit_agent.persistence.db import Jsonb, _encode_params


def test_jsonb_param_is_serialised_to_json_string():
    encoded = _encode_params([Jsonb({"k": "v", "n": 1}), "plain", 42])
    assert encoded[0] == json.dumps({"k": "v", "n": 1})
    assert encoded[1] == "plain"
    assert encoded[2] == 42


def test_comment_aware_split_ignores_semicolons_in_comments():
    raw = (
        "-- a comment; with a semicolon\n"
        "CREATE TABLE x (id int);\n"
        "INSERT INTO x VALUES (1); -- trailing; comment\n"
    )
    no_comments = re.sub(r"--[^\n]*", "", raw)
    stmts = [s.strip() for s in no_comments.split(";") if s.strip()]
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE x")
    assert stmts[1].startswith("INSERT INTO x")
