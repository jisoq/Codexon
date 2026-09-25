import json
import sqlite3
from datetime import datetime, timezone

from cachemonitor.core import Session
from cachemonitor.index import UsageIndex


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


def test_legacy_rate_limit_repeat_and_missing_usage():
    session = Session(TID, "home")
    item = event("event_msg", type="token_count", info={"total_token_usage": {"input_tokens": 10000},
                                                       "last_token_usage": {"input_tokens": 10000}})
    session.consume(item, 10001)
    session.consume(item, 10002)
    assert len(session.requests) == 1
    assert session.view(10002)["rate"] is None


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


def test_two_concurrent_sessions_have_independent_fallback(tmp_path):
    home, path = fixture_home(tmp_path)
    other = "87654321-abcd-abcd-abcd-123456789012"
    with sqlite3.connect(home / "logs_2.sqlite") as c:
        c.execute("insert into logs values(2,10000,0,'codex_core::client',?,'p1','falling back to HTTP')", (other,))
    index = UsageIndex([home],tmp_path/"index.sqlite")
    try:snapshot = index.poll(10001)
    finally:index.close()
    by_id = {s["id"]: s for s in snapshot["sessions"]}
    assert by_id[TID]["transport"] == "WebSocket"
    assert by_id[other]["transport"] == "HTTP/SSE"


def test_missing_databases_are_visible_errors_and_not_created(tmp_path):
    home=tmp_path/"home";home.mkdir()
    index=UsageIndex([home],tmp_path/"index.sqlite")
    try:snapshot=index.poll()
    finally:index.close()
    assert len(snapshot["errors"]) == 2
    assert list(home.iterdir()) == []


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
    monitor = UsageIndex([home],tmp_path/"index.sqlite")
    view = monitor.poll(10002)
    assert view["excluded_title_sessions"] == 1
    assert [s["id"] for s in view["sessions"]] == [TID]
    assert view["sessions"][0]["totals"]["session"]["total"] == 10010
    monitor.close()
    monitor = UsageIndex([home],tmp_path/"index.sqlite")
    assert monitor.poll(10002)["excluded_title_sessions"] == 1
    # A real persisted Luna task remains visible, even if it uses the same prompt.
    with sqlite3.connect(home / "state_5.sqlite") as c:
        c.execute("insert into threads values(?,?,?,?,?,?,?,?)", (helper, "", 10001, "Luna task", "", "gpt-5.6-luna", "openai", "project"))
    view = monitor.poll(10013)
    assert view["excluded_title_sessions"] == 0
    assert len(view["sessions"]) == 2
    monitor.close()
