import unittest

from sidecar.adapters.base import local_timestamp
from sidecar.adapters.kimi import KimiAdapter
from sidecar.model import Session


REAL_LOOP_RECORDS = (
    {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {
            "type": "content.part",
            "uuid": "think-1",
            "turnId": "0",
            "step": 1,
            "stepUuid": "step-1",
            "part": {"type": "think", "think": "I will create both files."},
        },
        "time": 1787429063194,
    },
    {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {
            "type": "tool.call",
            "uuid": "call-1",
            "turnId": "0",
            "step": 1,
            "stepUuid": "step-1",
            "toolCallId": "tool-write-1",
            "name": "Write",
            "args": {"path": "test1.txt", "content": "hello\n"},
        },
        "time": 1787429063221,
    },
    {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {
            "type": "tool.call",
            "uuid": "call-2",
            "turnId": "0",
            "step": 1,
            "stepUuid": "step-1",
            "toolCallId": "tool-write-2",
            "name": "Write",
            "args": {"path": "test2.txt", "content": "world\n"},
        },
        "time": 1787429063222,
    },
    {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {
            "type": "tool.call",
            "uuid": "call-3",
            "turnId": "0",
            "step": 1,
            "stepUuid": "step-1",
            "toolCallId": "tool-bash",
            "name": "Bash",
            "args": {
                "command": "cat test1.txt test2.txt",
                "cwd": "/private/tmp/kimi_graytest",
            },
        },
        "time": 1787429063222,
    },
    {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {
            "type": "tool.result",
            "parentUuid": "call-1",
            "toolCallId": "tool-write-1",
            "result": {"output": "Wrote 6 bytes to test1.txt"},
        },
        "time": 1787429063230,
    },
    {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {
            "type": "tool.result",
            "parentUuid": "call-2",
            "toolCallId": "tool-write-2",
            "result": {"output": "Wrote 6 bytes to test2.txt"},
        },
        "time": 1787429063230,
    },
    {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {
            "type": "tool.result",
            "parentUuid": "call-3",
            "toolCallId": "tool-bash",
            "result": {"output": "hello\nworld\n"},
        },
        "time": 1787429063236,
    },
    {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {
            "type": "content.part",
            "uuid": "think-2",
            "turnId": "0",
            "step": 2,
            "stepUuid": "step-2",
            "part": {"type": "think", "think": "All done."},
        },
        "time": 1787429064595,
    },
    {
        "type": "context.append_loop_event",
        "agentId": "main",
        "event": {
            "type": "content.part",
            "uuid": "text-1",
            "turnId": "0",
            "step": 2,
            "stepUuid": "step-2",
            "part": {"type": "text", "text": "Created both files."},
        },
        "time": 1787429064595,
    },
)


def make_session():
    return Session(
        agent="kimi",
        session_id="session_fbeaa814",
        project="/private/tmp/kimi_graytest",
        transcript="/tmp/wire.jsonl",
        updated_at=1787429064.595,
    )


class KimiNormalizeTests(unittest.TestCase):
    def setUp(self):
        self.adapter = KimiAdapter()
        self.session = make_session()

    def normalize_all(self, records):
        return [
            event
            for record in records
            for event in self.adapter.normalize(record, self.session)
        ]

    def test_real_loop_records_emit_assistant_thinking_and_exact_tool_events(self):
        events = self.normalize_all(REAL_LOOP_RECORDS)

        self.assertEqual(
            [
                "thinking",
                "tool_call",
                "tool_call",
                "tool_call",
                "tool_result",
                "tool_result",
                "tool_result",
                "thinking",
                "assistant",
            ],
            [event.kind for event in events],
        )
        self.assertEqual(
            ["tool-write-1", "tool-write-2", "tool-bash"],
            [event.extra["call_id"] for event in events if event.kind == "tool_call"],
        )
        self.assertEqual(
            ["tool-write-1", "tool-write-2", "tool-bash"],
            [event.extra["call_id"] for event in events if event.kind == "tool_result"],
        )
        self.assertEqual("hello world", events[6].text)
        self.assertEqual("Created both files.", events[-1].text)

    def test_loop_events_preserve_timestamp_session_and_nested_metadata(self):
        event = list(self.adapter.normalize(REAL_LOOP_RECORDS[1], self.session))[0]

        self.assertEqual(local_timestamp(1787429063221), event.ts)
        self.assertEqual("kimi", event.agent)
        self.assertEqual("session_fbeaa814", event.session_id)
        self.assertEqual(
            {
                "native_type": "context.append_loop_event",
                "agent_id": "main",
                "loop_event_type": "tool.call",
                "event_uuid": "call-1",
                "turn_id": "0",
                "step": 1,
                "step_uuid": "step-1",
                "call_id": "tool-write-1",
                "tool_name": "Write",
            },
            event.extra,
        )

        result = list(self.adapter.normalize(REAL_LOOP_RECORDS[4], self.session))[0]
        self.assertEqual("call-1", result.extra["parent_uuid"])
        self.assertEqual("tool-write-1", result.extra["call_id"])

    def test_context_append_message_normalization_is_preserved(self):
        record = {
            "type": "context.append_message",
            "agentId": "main",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "legacy answer"}],
                "toolCalls": [
                    {
                        "id": "legacy-call",
                        "name": "Read",
                        "arguments": {"path": "old.txt"},
                    }
                ],
            },
            "time": 1787429064000,
        }

        events = list(self.adapter.normalize(record, self.session))

        self.assertEqual(["assistant", "tool_call"], [event.kind for event in events])
        self.assertEqual("legacy answer", events[0].text)
        self.assertEqual("legacy-call", events[1].extra["call_id"])

    def test_message_content_blocks_preserve_kinds_text_and_extra_fields(self):
        record = {
            "type": "context.append_message",
            "agentId": "worker-1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "final answer"},
                    {"type": "thinking", "thinking": "careful thought"},
                    {
                        "type": "tool_use",
                        "toolName": "Read",
                        "input": {"path": "fixture.txt"},
                    },
                    {
                        "type": "tool_result",
                        "content": {"output": "fixture\ncontents"},
                    },
                    {"type": "unknown", "text": "ignored"},
                ],
            },
            "time": 1787429064000,
        }

        events = list(self.adapter.normalize(record, self.session))

        self.assertEqual(
            ["assistant", "thinking", "tool_call", "tool_result"],
            [event.kind for event in events],
        )
        self.assertEqual(
            [
                "final answer",
                "careful thought",
                'Read {"path":"fixture.txt"}',
                "fixture contents",
            ],
            [event.text for event in events],
        )
        self.assertEqual(
            [
                {
                    "native_type": "context.append_message",
                    "agent_id": "worker-1",
                }
            ]
            * 4,
            [event.extra for event in events],
        )

    def test_message_content_block_truncation_limits_are_preserved(self):
        record = {
            "type": "context.append_message",
            "agentId": "main",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "a" * 121},
                    {"type": "thinking", "thinking": "b" * 81},
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "arguments": "c" * 121,
                    },
                    {"type": "tool_result", "content": "d" * 81},
                ],
            },
            "time": 1787429064000,
        }

        events = list(self.adapter.normalize(record, self.session))

        self.assertEqual([120, 80, 120, 80], [len(event.text) for event in events])
        self.assertTrue(all(event.text.endswith("…") for event in events))

    def test_equivalent_context_messages_are_deduped_without_losing_tools(self):
        records = (
            {
                "type": "turn.prompt",
                "agentId": "main",
                "input": [{"type": "text", "text": "one prompt"}],
                "origin": {"kind": "user"},
                "time": 1787429060190,
            },
            {
                "type": "context.append_message",
                "agentId": "main",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "one prompt"}],
                },
                "time": 1787429060191,
            },
            {
                "type": "context.append_loop_event",
                "agentId": "main",
                "event": {
                    "type": "content.part",
                    "uuid": "answer",
                    "turnId": "0",
                    "step": 1,
                    "stepUuid": "step-1",
                    "part": {"type": "text", "text": "one answer"},
                },
                "time": 1787429064595,
            },
            {
                "type": "context.append_message",
                "agentId": "main",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "one answer"}],
                    "toolCalls": [
                        {
                            "id": "must-survive",
                            "name": "Read",
                            "arguments": {"path": "file.txt"},
                        }
                    ],
                },
                "time": 1787429064596,
            },
        )

        events = self.normalize_all(records)

        self.assertEqual(
            ["user", "assistant", "tool_call"],
            [event.kind for event in events],
        )
        self.assertEqual("must-survive", events[-1].extra["call_id"])

    def test_llm_request_with_prompt_and_tool_snapshots_is_ignored(self):
        record = {
            "type": "llm.request",
            "agentId": "main",
            "kind": "loop",
            "model": "kimi-for-coding",
            "systemPrompt": "large system prompt" * 1000,
            "tools": [
                {
                    "name": "Write",
                    "description": "large tool description" * 1000,
                    "inputSchema": {"type": "object"},
                }
            ],
            "messageCount": 6,
            "time": 1787429063242,
        }

        self.assertEqual([], list(self.adapter.normalize(record, self.session)))


if __name__ == "__main__":
    unittest.main()
