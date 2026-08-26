#!/usr/bin/env python3
"""Deterministic, model-free fake Kimi ACP server for protocol tests."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


SCENARIO = os.environ.get("FAKE_KIMI_ACP_SCENARIO", "valid")
SESSION_ID = os.environ.get("FAKE_KIMI_ACP_SESSION_ID", "session-1")
ARTIFACT_DIR = os.environ.get("FAKE_KIMI_ACP_ARTIFACT_DIR")


def encode(value):
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def emit(value):
    os.write(1, encode(value) + b"\n")


def emit_raw(value):
    os.write(1, value)


def response(request_id, result):
    emit({"jsonrpc": "2.0", "id": request_id, "result": result})


def error_response(request_id, code=-32603, data=None):
    error = {"code": code, "message": "synthetic failure"}
    if data is not None:
        error["data"] = data
    emit({"jsonrpc": "2.0", "id": request_id, "error": error})


def read_request():
    line = sys.stdin.buffer.readline()
    if not line:
        raise EOFError
    return json.loads(line.decode("utf-8"))


def require_request(expected_id, expected_method):
    value = read_request()
    assert value["jsonrpc"] == "2.0"
    assert value["id"] == expected_id
    assert value["method"] == expected_method
    return value


def initialize():
    request = require_request(1, "initialize")
    assert request["params"]["protocolVersion"] == 1
    assert request["params"]["clientCapabilities"] == {}
    assert request["params"]["clientInfo"]["name"] == "agent-sidecar"
    result = {
        "protocolVersion": 1,
        "agentCapabilities": {
            "loadSession": True,
            "sessionCapabilities": {"list": {}, "resume": {}},
        },
    }
    if SCENARIO == "timeout_before":
        time.sleep(60)
    if SCENARIO == "malformed_before":
        emit_raw(b"{not-json}\n")
        time.sleep(60)
    if SCENARIO == "bad_utf8":
        emit_raw(b'{"jsonrpc":"2.0","id":1,"result":"\xff"}\n')
        time.sleep(60)
    if SCENARIO == "duplicate_key":
        emit_raw(
            b'{"jsonrpc":"2.0","id":1,"id":1,'
            b'"result":{"protocolVersion":1}}\n'
        )
        time.sleep(60)
    if SCENARIO == "nonfinite":
        emit_raw(b'{"jsonrpc":"2.0","id":1,"result":{"value":NaN}}\n')
        time.sleep(60)
    if SCENARIO == "excess_depth":
        emit_raw(
            (
                '{"jsonrpc":"2.0","id":1,"result":'
                + "[" * 40
                + "0"
                + "]" * 40
                + "}\n"
            ).encode("ascii")
        )
        time.sleep(60)
    if SCENARIO == "excess_items":
        emit_raw(
            (
                '{"jsonrpc":"2.0","id":1,"result":['
                + ",".join("0" for _index in range(8200))
                + "]}\n"
            ).encode("ascii")
        )
        time.sleep(60)
    if SCENARIO == "oversized_before":
        emit_raw(b"x" * (256 * 1024 + 1))
        time.sleep(60)
    if SCENARIO == "unterminated":
        emit_raw(b'{"jsonrpc":"2.0","id":1')
        return False
    if SCENARIO == "eof":
        return False
    if SCENARIO == "missing_capability":
        del result["agentCapabilities"]["sessionCapabilities"]["resume"]
    if SCENARIO == "protocol_mismatch":
        result["protocolVersion"] = 2
    if SCENARIO == "split":
        payload = encode({"jsonrpc": "2.0", "id": 1, "result": result}) + b"\n"
        midpoint = len(payload) // 2
        emit_raw(payload[:midpoint])
        time.sleep(0.02)
        emit_raw(payload[midpoint:])
    elif SCENARIO == "coalesced":
        update = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": SESSION_ID,
                "update": {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [],
                },
            },
        }
        emit_raw(encode(update) + b"\n" + encode({
            "jsonrpc": "2.0",
            "id": 1,
            "result": result,
        }) + b"\n")
    else:
        response(1, result)
    return True


def list_session():
    request = require_request(2, "session/list")
    cwd = request["params"]["cwd"]
    sessions = [{"sessionId": SESSION_ID, "cwd": cwd, "title": "synthetic"}]
    if SCENARIO == "list_absent":
        sessions = [{"sessionId": "other-session", "cwd": cwd}]
    elif SCENARIO == "list_duplicate":
        sessions.append({"sessionId": SESSION_ID, "cwd": cwd})
    elif SCENARIO == "list_cwd_mismatch":
        sessions[0]["cwd"] = str(Path(cwd).parent)
    response(2, {"sessions": sessions})


def resume():
    request = require_request(3, "session/resume")
    assert request["params"] == {
        "sessionId": SESSION_ID,
        "cwd": request["params"]["cwd"],
        "mcpServers": [],
    }
    if SCENARIO == "resume_error":
        error_response(3, -32602)
    else:
        response(3, {})


def set_mode():
    request = require_request(4, "session/set_mode")
    assert request["params"] == {
        "sessionId": SESSION_ID,
        "modeId": "default",
    }
    if SCENARIO == "set_mode_error":
        error_response(4, -32602)
    elif SCENARIO == "set_mode_nonempty":
        response(4, {"mode": "default"})
    else:
        response(4, {})


def permission(question=False):
    request_id = "question-1" if question else "permission-1"
    option = {
        "optionId": "q1_skip" if question else "reject",
        "name": "Dismiss" if question else "Reject",
        "kind": "reject_once",
    }
    emit(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": SESSION_ID,
                "toolCall": {
                    "toolCallId": "ask-1" if question else "tool-1",
                    "title": "synthetic",
                    "rawInput": {"kind": "question" if question else "approval"},
                },
                "options": [option],
            },
        }
    )
    reply = read_request()
    assert reply == {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"outcome": {"outcome": "cancelled"}},
    }


def write_prompt_digest(message):
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    expected = os.environ.get("FAKE_KIMI_ACP_EXPECTED_DIGEST")
    if expected is not None:
        assert digest == expected
    if ARTIFACT_DIR is None:
        return
    root = Path(ARTIFACT_DIR)
    assert root.is_absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "prompt.sha256").write_text(digest + "\n", encoding="ascii")
    (root / "scenario.json").write_text(
        json.dumps(
            {"scenario": SCENARIO, "prompt_sha256": digest},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def spawn_lingering_child(escape=False):
    code = (
        "import os,time;"
        + ("os.setsid();os.closerange(0,256);" if escape else "")
        + "time.sleep(60)"
    )
    subprocess.Popen([sys.executable, "-c", code])


def prompt():
    request = require_request(5, "session/prompt")
    params = request["params"]
    assert params["sessionId"] == SESSION_ID
    assert isinstance(params["messageId"], str) and params["messageId"]
    blocks = params["prompt"]
    assert len(blocks) == 1 and blocks[0]["type"] == "text"
    message = blocks[0]["text"]
    assert isinstance(message, str)
    write_prompt_digest(message)

    if SCENARIO == "permission":
        permission(False)
    elif SCENARIO == "question":
        permission(True)
    elif SCENARIO == "unknown_reverse":
        emit(
            {
                "jsonrpc": "2.0",
                "id": "reverse-1",
                "method": "fs/read_text_file",
                "params": {"path": "/synthetic"},
            }
        )
        reply = read_request()
        assert reply["id"] == "reverse-1"
        assert reply["error"]["code"] == -32601
        time.sleep(60)
    elif SCENARIO == "interleaved_update":
        emit(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": SESSION_ID,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "synthetic reply"},
                    },
                },
            }
        )
    elif SCENARIO == "aggregate_overflow":
        padding = "x" * (240 * 1024)
        update = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": SESSION_ID,
                "update": {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [],
                    "_meta": {"padding": padding},
                },
            },
        }
        for _index in range(20):
            emit(update)
        time.sleep(60)
    elif SCENARIO == "stderr_overflow":
        os.write(2, b"e" * (64 * 1024 + 1))
        time.sleep(60)
    elif SCENARIO == "malformed_after":
        emit_raw(b"{not-json}\n")
        time.sleep(60)
    elif SCENARIO == "oversized_after":
        emit_raw(b"x" * (256 * 1024 + 1))
        time.sleep(60)
    elif SCENARIO == "timeout_after_full":
        while True:
            value = read_request()
            if value.get("method") == "session/cancel":
                break
        return
    elif SCENARIO == "late_settlement":
        cancel = read_request()
        assert cancel["method"] == "session/cancel"
        response(5, {"stopReason": "cancelled"})
        return
    elif SCENARIO == "busy":
        error_response(
            5,
            -32600,
            {"code": "turn.agent_busy", "details": {"turnId": 7}},
        )
        return
    elif SCENARIO == "child_lingers":
        spawn_lingering_child(False)
    elif SCENARIO == "child_exits":
        subprocess.Popen([sys.executable, "-c", "pass"]).wait()
    elif SCENARIO == "child_escapes":
        spawn_lingering_child(True)
    elif SCENARIO == "forks" and hasattr(os, "fork"):
        pid = os.fork()
        if pid == 0:
            time.sleep(60)
            os._exit(0)
    elif SCENARIO == "duplicate_response":
        response(5, {"stopReason": "end_turn"})
        response(5, {"stopReason": "end_turn"})
        return
    elif SCENARIO == "unknown_response":
        response(99, {})
        time.sleep(60)

    stop_reason = "end_turn"
    if SCENARIO == "clean_cancelled":
        stop_reason = "cancelled"
    elif SCENARIO == "clean_refusal":
        stop_reason = "refusal"
    response(5, {"stopReason": stop_reason})


def main():
    if sys.argv[1:] != ["acp"]:
        return 64
    signal.signal(signal.SIGTERM, lambda _signum, _frame: raise_exit(143))
    if not initialize():
        return 0
    list_session()
    resume()
    if SCENARIO == "resume_error":
        return 0
    set_mode()
    if SCENARIO in ("set_mode_error", "set_mode_nonempty"):
        return 0
    prompt()
    return 0


def raise_exit(code):
    raise SystemExit(code)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, EOFError, json.JSONDecodeError):
        raise SystemExit(70)
