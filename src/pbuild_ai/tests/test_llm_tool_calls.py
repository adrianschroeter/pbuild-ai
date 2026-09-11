import unittest
import json
from pbuild_ai.llm_client import LlmAnalyzer


class TestQwenToolCallExtraction(unittest.TestCase):
    """Test extraction of tool calls from qwen3.6 output format."""

    def setUp(self):
        self.ai = LlmAnalyzer(host="http://localhost:11434", model="test", debug=False)

    def test_qwen_inline_format_with_newlines(self):
        """Test qwen3.6 inline format with literal newlines in strings."""
        content = '{"name": "edit_file", "path": "ollama.spec", "old_string": "Version:        0.33.1\nRelease:        0\nSummary:", "new_string": "Version:        0.33.3\nRelease:        1\nSummary:"}'
        calls = self.ai._extract_tool_calls_from_content(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "edit_file")
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertIn("path", args)
        self.assertIn("old_string", args)
        self.assertIn("new_string", args)
        # Newlines are escaped to \\n in the extracted JSON
        self.assertIn("\\nRelease:", args["old_string"])

    def test_qwen_arguments_format_with_newlines(self):
        """Test qwen3.6 arguments format with literal newlines."""
        content = '{"name": "edit_file", "arguments": {"path": "ollama.spec", "old_string": "Version: 0.33.1\nRelease: 0", "new_string": "Version: 0.33.3\nRelease: 1"}}'
        calls = self.ai._extract_tool_calls_from_content(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "edit_file")
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["path"], "ollama.spec")

    def test_qwen_args_format(self):
        """Test qwen3.6 args format (alternative to arguments)."""
        content = '{"name": "run_tool_script", "args": {"script_name": ".agents/skills/update_references.sh"}}'
        calls = self.ai._extract_tool_calls_from_content(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "run_tool_script")
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["script_name"], ".agents/skills/update_references.sh")

    def test_multiple_tool_calls_concatenated(self):
        """Test multiple tool calls concatenated with newlines."""
        content = (
            '{"name": "edit_file", "arguments": {"path": "ollama.spec", "old_string": "Version: 0.33.1\nRelease: 0", "new_string": "Version: 0.33.3\nRelease: 1"}}\n'
            '{"name": "edit_file", "args": {"path": "ollama.changes", "old_string": "%changelog"}}\n'
            '{"name": "run_tool_script", "arguments": {"script_name": ".agents/skills/update_references.sh"}}'
        )
        calls = self.ai._extract_tool_calls_from_content(content)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["function"]["name"], "edit_file")
        self.assertEqual(calls[1]["function"]["name"], "edit_file")
        self.assertEqual(calls[2]["function"]["name"], "run_tool_script")

    def test_mixed_with_thinking_text(self):
        """Test tool calls embedded with thinking/reasoning text."""
        content = (
            'Here is my analysis...\n'
            '{"name": "edit_file", "arguments": {"path": "ollama.spec", "old_string": "Version: 0.33.1", "new_string": "Version: 0.33.3"}}\n'
            'More reasoning here...\n'
            '{"name": "run_tool_script", "args": {"script_name": ".agents/skills/update_references.sh"}}'
        )
        calls = self.ai._extract_tool_calls_from_content(content)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["function"]["name"], "edit_file")
        self.assertEqual(calls[1]["function"]["name"], "run_tool_script")

    def test_tool_calls_section_end_marker(self):
        """Test handling of <|tool_calls_section_end|> marker."""
        content = (
            '{"name": "edit_file", "arguments": {"path": "ollama.spec", "old_string": "v1", "new_string": "v2"}}\n'
            '<|tool_calls_section_end|>\n'
            '{"name": "download_file", "arguments": {"url": "http://example.com/file.tar.gz", "filename": "file.tar.gz"}}'
        )
        calls = self.ai._extract_tool_calls_from_content(content)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["function"]["name"], "edit_file")
        self.assertEqual(calls[1]["function"]["name"], "download_file")

    def test_openai_standard_format(self):
        """Test standard OpenAI tool_calls format (for compatibility)."""
        # This is the format returned in message.tool_calls, not content
        # But our extractor should handle it if it appears in content
        content = '{"name": "write_file", "arguments": {"path": "test.txt", "content": "hello world"}}'
        calls = self.ai._extract_tool_calls_from_content(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "write_file")

    def test_no_tool_calls(self):
        """Test content with no tool calls returns empty list."""
        content = "This is just a regular text response without any tool calls."
        calls = self.ai._extract_tool_calls_from_content(content)
        self.assertEqual(len(calls), 0)

    def test_malformed_json_ignored(self):
        """Test that malformed JSON doesn't break extraction."""
        content = (
            '{"name": "edit_file", "arguments": {"path": "ollama.spec"}}\n'
            'this is not json\n'
            '{"name": "run_tool_script", "args": {"script_name": "test.sh"}}'
        )
        calls = self.ai._extract_tool_calls_from_content(content)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()