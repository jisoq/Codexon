import json
import sqlite3
from datetime import datetime, timezone

from cachemonitor.core import Monitor, Session, WINDOW, SESSION_WINDOW, parse_transport


TID = "12345678-abcd-abcd-abcd-123456789012"


def ts(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def event(kind, time=10000, **payload):
    return {"type": kind, "timestamp": ts(time), "payload": payload}


def usage(response="r1", time=10000, input=10000, cached=9000, tid=TID):
    return event("token_usage_record", time, thread_id=tid, response_id=response,
                 usage={"input_tokens": input, "cached_input_tokens": cached, "output_tokens": 10})


def log(body, target="feedback_tags", tid=TID, timestamp=9999):
    return {"ts": timestamp, "ts_nanos": 0, "target": target, "thread_id": tid,
            "process_uuid": "p1", "feedback_log_body": body}


def test_per_session_transport_and_tool_text_not_evidence():
    ws = parse_transport(log('turn{thread_id=' + TID + '}:request{transport="responses_websocket" api.path="/responses"}: auth_header_attached=true'))
    http = parse_transport(log('turn{thread_id=' + TID + '}: falling back to HTTP', "codex_core::client"))
    assert ws[1].kind == "WebSocket"
    assert http[1].kind == "HTTP/SSE"
    assert parse_transport(log('ToolCall: falling back to HTTP', "codex_core::stream_events_utils")) is None
    assert parse_transport(log('ToolCall: transport="responses_websocket" auth_header_attached=true')) is None
    assert parse_transport(log('response{transport="remote_control_websocket"}: auth_header_attached=true')) is None


def test_unassigned_not_guessed_and_url_secrets_removed():
    parsed = parse_transport(log('connecting to websocket: wss://user:secret@example.com/responses?token=secret',
                                "codex_api::endpoint::responses_websocket", tid=None))
    assert parsed[0] is None
    assert parsed[1].endpoint == "wss://example.com/responses"
    assert "secret" not in str(parsed)


def test_cached_usage_deduplicated_and_fork_skipped():
    session = Session(TID, "home")
    session.consume(usage(), 10000)
    session.consume(usage(), 10001)
    session.consume(usage("fork", tid="other"), 10001)
    session.consume(event("event_msg", type="token_count", info={"total_token_usage": {"input_tokens": 10000},
                                  "last_token_usage": {"input_tokens": 10000, "cached_input_tokens": 9000}}), 10001)
    assert len(session.requests) == 1
    assert session.view(10001)["rate"] == 90


def test_legacy_rate_limit_repeat_and_missing_usage():
    session = Session(TID, "home")
    item = event("event_msg", type="token_count", info={"total_token_usage": {"input_tokens": 10000},
                                                       "last_token_usage": {"input_tokens": 10000}})
    session.consume(item, 10001)
    session.consume(item, 10002)
    assert len(session.requests) == 1
    assert session.view(10002)["rate"] is None


def test_weighted_rate_drop_and_first_zero_not_warning():
    session = Session(TID, "home")
    session.consume(usage("first", input=5000, cached=0), 10000)
    assert not session.view(10000)["warning"]
    session.consume(usage("second", time=10001, input=20000, cached=19000), 10001)
    session.consume(usage("third", time=10002, input=25000, cached=0), 10002)
    v = session.view(10002)
    assert v["weighted_rate"] == 38
    assert not v["warning"]
    assert v['cache_misses']['count']==2  # A fact, including the first call, not an error alert.
    assert v["remaining"] == WINDOW


def fixture_home(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    path = home / "session.jsonl"
    path.write_text(json.dumps(usage()) + "\n", encoding="utf8")
    with sqlite3.connect(home / "state_5.sqlite") as c:
        c.execute("create table threads(id text, rollout_path text, updated_at integer, name text, title text, model text, model_provider text, cwd text)")
        c.execute("insert into threads values(?,?,?,?,?,?,?,?)", (TID, str(path), 10000, "작업 이름", "긴 원문", "test", "openai", "project"))
    with sqlite3.connect(home / "logs_2.sqlite") as c:
        c.execute("create table logs(id integer primary key, ts integer, ts_nanos integer, target text, thread_id text, process_uuid text, feedback_log_body text)")
        c.execute("insert into logs values(1,9999,0,'feedback_tags',?,'p1',?)", (TID, 'request{transport="responses_websocket" api.path="/responses"}: auth_header_attached=true'))
    return home, path


def test_integration_partial_lines_restart_and_expiry(tmp_path):
    home, path = fixture_home(tmp_path)
    monitor = Monitor([home])
    first = monitor.poll(10001)
    assert not first["errors"]
    assert first["sessions"][0]["title"] == "작업 이름"
    assert first["sessions"][0]["requests"][0]["transport"] == "WebSocket"
    second = (json.dumps(usage("r2", time=10010), ensure_ascii=False) + "\n").encode()
    with path.open("ab") as h:
        h.write(second[:60])
    assert len(monitor.poll(10011)["sessions"][0]["requests"]) == 1
    with path.open("ab") as h:
        h.write(second[60:])
    assert len(monitor.poll(10012)["sessions"][0]["requests"]) == 2
    assert len(monitor.poll(10013)["sessions"][0]["requests"]) == 2
    assert len(Monitor([home]).poll(10013)["sessions"][0]["requests"]) == 2
    assert monitor.poll(10010 + WINDOW + 1)["sessions"][0]["requests"] == []
    assert monitor.poll(10010 + SESSION_WINDOW + 1)["sessions"] == []


def test_two_concurrent_sessions_have_independent_fallback(tmp_path):
    home, path = fixture_home(tmp_path)
    other = "87654321-abcd-abcd-abcd-123456789012"
    with sqlite3.connect(home / "logs_2.sqlite") as c:
        c.execute("insert into logs values(2,10000,0,'codex_core::client',?,'p1','falling back to HTTP')", (other,))
    snapshot = Monitor([home]).poll(10001)
    by_id = {s["id"]: s for s in snapshot["sessions"]}
    assert by_id[TID]["transport"] == "WebSocket"
    assert by_id[other]["transport"] == "HTTP/SSE"


def test_missing_databases_are_visible_errors_and_not_created(tmp_path):
    snapshot = Monitor([tmp_path]).poll()
    assert len(snapshot["errors"]) == 2
    assert list(tmp_path.iterdir()) == []


def test_rollout_truncation_recovers_without_duplicates(tmp_path):
    home, path = fixture_home(tmp_path)
    monitor = Monitor([home])
    monitor.poll(10001)
    path.write_text("", encoding="utf8")
    monitor.poll(10002)
    path.write_text(json.dumps(usage()) + "\n" + json.dumps(usage("r2", time=10003)) + "\n", encoding="utf8")
    assert len(monitor.poll(10004)["sessions"][0]["requests"]) == 2


def test_effort_accounting_and_unclassified_lifetime():
    session = Session(TID, "home")
    for index, effort in enumerate(("low", "high")):
        turn = f"t{index}"
        session.consume(event("turn_context", turn_id=turn, model="m", effort=effort), 10000)
        session.consume(event("token_usage_record", 10000 + index, thread_id=TID, turn_id=turn,
                              response_id=turn, usage={"input_tokens": 100, "cached_input_tokens": 80,
                              "output_tokens": 20, "reasoning_output_tokens": 15},
                              thread_token_usage={"input_tokens": 1000 + index * 100,
                              "cached_input_tokens": 800 + index * 80, "output_tokens": 200 + index * 20,
                              "reasoning_output_tokens": 150 + index * 15}), 10002)
    v = session.view(10002)
    assert v["totals"]["session"]["total"] == 240
    assert v["totals"]["30m"]["total"] == 240
    assert v["totals"]["30m"]["reasoning"] == 30
    assert {g["effort"] for g in v["groups"]["30m"]} == {"low", "high"}
    assert sum(g["total"] for g in v["groups"]["session"]) == 240
    assert v["unclassified"]["total"] == 1080
    assert all(g['count']==1 for g in v['groups']['session'])


def test_missing_effort_and_invalid_subsets_are_unknown():
    from cachemonitor.core import usage_values
    values = usage_values({"input_tokens": 100, "cached_input_tokens": 101,
                           "output_tokens": 20, "reasoning_output_tokens": 21})
    assert values["total"] == 120
    assert values["cached"] is None and values["reasoning"] is None
    session = Session(TID, "home")
    session.consume(usage(), 10001)
    assert session.view(10001)["groups"]["session"][0]["effort"] == "미확인"


def test_title_helper_excluded_but_regular_luna_preserved(tmp_path):
    from cachemonitor.core import TITLE_HANDLER, TITLE_PROMPT, is_title_request
    home, path = fixture_home(tmp_path)
    helper = "87654321-abcd-abcd-abcd-123456789012"
    body = ('session_loop{thread_id=' + helper + '}: Submission sub=Submission { id: "t", '
            'op: TurnInput { request: TurnInputRequest { input: UserInput { content: [Text { text: "'
            + TITLE_PROMPT + '" }], "required": Array [String("title"), String("description")]')
    row = {"thread_id": helper, "target": TITLE_HANDLER, "feedback_log_body": body}
    assert is_title_request(row)
    assert not is_title_request({**row, "target": "codex_core::stream_events_utils"})
    assert not is_title_request({**row, "feedback_log_body": "tool output: " + body})
    with sqlite3.connect(home / "logs_2.sqlite") as c:
        c.execute("insert into logs values(2,10000,0,?,?,?,?)", (TITLE_HANDLER, helper, "p1", body))
        c.execute("insert into logs values(3,10001,0,'feedback_tags',?,'p1',?)", (helper,
                  'turn{model=gpt-5.6-luna}:request{transport="responses_websocket" api.path="/responses"}: auth_header_attached=true'))
    monitor = Monitor([home])
    view = monitor.poll(10002)
    assert view["excluded_title_sessions"] == 1
    assert [s["id"] for s in view["sessions"]] == [TID]
    assert view["sessions"][0]["totals"]["session"]["total"] == 10010
    assert Monitor([home]).poll(10002)["excluded_title_sessions"] == 1
    # A real persisted Luna task remains visible, even if it uses the same prompt.
    with sqlite3.connect(home / "state_5.sqlite") as c:
        c.execute("insert into threads values(?,?,?,?,?,?,?,?)", (helper, "", 10001, "Luna task", "", "gpt-5.6-luna", "openai", "project"))
    view = monitor.poll(10003)
    assert view["excluded_title_sessions"] == 0
    assert len(view["sessions"]) == 2


def test_combined_groups_preserve_unknown_count_and_missing_metrics():
    from cachemonitor.core import combined_usage_groups, summarize
    from cachemonitor.app import average_tokens
    a = {"model": "m", "effort": "high", **summarize([{"total": 100, "input": 80}])}
    b = {"model": "m", "effort": "high", **summarize([{"total": 300, "input": 280, "cached": 200}])}
    old = {"model": "이전 기록", "effort": "미확인", **summarize([{"total": 900}]), "count": None}
    merged = combined_usage_groups([{"groups": {"session": [a, old]}}, {"groups": {"session": [b]}}], "session")
    assert sum(g["total"] for g in merged) == 1300
    assert merged[0]["count"] is None
    assert average_tokens(merged[0]) == "—"
    assert average_tokens(merged[1]) == "200.0"
    assert average_tokens(merged[1], "cached") == "200.0"
