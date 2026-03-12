import json
import os
import sys
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestLocalToolCallParsing(unittest.TestCase):
    def test_parses_pure_tool_call_blocks(self):
        from ouroboros.llm import LLMClient

        msg = {
            "content": """
<tool_call>
{"name": "repo_read", "arguments": {"path": "README.md"}}
</tool_call>
<tool_call>
{"name": "repo_write", "arguments": {"path": "notes.txt", "content": "hello"}}
</tool_call>
""",
            "tool_calls": [],
        }

        parsed = LLMClient._parse_tool_calls_from_content(
            msg,
            {"repo_read", "repo_write"},
        )

        self.assertEqual(len(parsed["tool_calls"]), 2)
        self.assertIsNone(parsed["content"])
        self.assertEqual(parsed["tool_calls"][0]["function"]["name"], "repo_read")
        self.assertEqual(
            json.loads(parsed["tool_calls"][0]["function"]["arguments"]),
            {"path": "README.md"},
        )

    def test_rejects_mixed_prose_and_tool_calls(self):
        from ouroboros.llm import LLMClient

        msg = {
            "content": """
Sure, I will use the tool now.

<tool_call>
{"name": "repo_read", "arguments": {"path": "README.md"}}
</tool_call>
""",
            "tool_calls": [],
        }

        parsed = LLMClient._parse_tool_calls_from_content(msg, {"repo_read"})

        self.assertEqual(parsed, msg)

    def test_rejects_unknown_tool_names(self):
        from ouroboros.llm import LLMClient

        msg = {
            "content": """
<tool_call>
{"name": "repo_delete_everything", "arguments": {}}
</tool_call>
""",
            "tool_calls": [],
        }

        parsed = LLMClient._parse_tool_calls_from_content(msg, {"repo_read"})

        self.assertEqual(parsed, msg)

    def test_rejects_non_object_arguments(self):
        from ouroboros.llm import LLMClient

        msg = {
            "content": """
<tool_call>
{"name": "repo_read", "arguments": "README.md"}
</tool_call>
""",
            "tool_calls": [],
        }

        parsed = LLMClient._parse_tool_calls_from_content(msg, {"repo_read"})

        self.assertEqual(parsed, msg)
