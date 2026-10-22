import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pbuild_ai.llm_client import LlmAnalyzer

MODELS_PAYLOAD = {
    "object": "list",
    "data": [
        {
            "id": "Qwen3.8 Flash Next",
            "object": "model",
            "created": 1792661822,
            "owned_by": "gufo",
            "context_length": 262144,
        }
    ],
}


def _make_server(routes):
    """Start a throwaway HTTP server answering the paths in `routes`
    (path -> (status, payload dict)); every other path returns 404."""
    class Handler(BaseHTTPRequestHandler):
        def _reply(self):
            status, payload = routes.get(self.path, (404, {
                "error": {"message": "no route for this path",
                          "type": "invalid_request_error",
                          "code": "not_found"}}))
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply()

        def do_POST(self):
            self._reply()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _stop_server(server):
    server.shutdown()
    server.server_close()


class TestOpenAIAutoDetect(unittest.TestCase):
    """A plain host (no /v1) that is not Ollama but serves /v1/models must
    be used in OpenAI mode — before, pbuild-ai kept calling the Ollama-native
    /api/generate and the server answered 404 'no route for this path'."""

    def test_plain_openai_server_detected(self):
        server, url = _make_server({"/v1/models": (200, MODELS_PAYLOAD)})
        self.addCleanup(_stop_server, server)
        ai = LlmAnalyzer(host=url, model="Qwen3.8 Flash Next", debug=False)
        self.assertTrue(ai._openai_mode)
        self.assertFalse(ai._is_ollama)
        self.assertEqual(ai.api_url, f"{url}/v1/chat/completions")
        self.assertEqual(ai.chat_api_url, f"{url}/v1/chat/completions")

    def test_explicit_model_gets_probed_context_length(self):
        server, url = _make_server({"/v1/models": (200, MODELS_PAYLOAD)})
        self.addCleanup(_stop_server, server)
        ai = LlmAnalyzer(host=url, model="Qwen3.8 Flash Next", debug=False)
        self.assertEqual(ai.max_tokens, 262144)

    def test_nonmatching_model_keeps_default_context(self):
        server, url = _make_server({"/v1/models": (200, MODELS_PAYLOAD)})
        self.addCleanup(_stop_server, server)
        ai = LlmAnalyzer(host=url, model="Gufo Qwen3.8 Flash Next", debug=False)
        self.assertTrue(ai._openai_mode)
        self.assertEqual(ai.max_tokens, 32768)

    def test_default_model_discovered_with_context(self):
        server, url = _make_server({"/v1/models": (200, MODELS_PAYLOAD)})
        self.addCleanup(_stop_server, server)
        ai = LlmAnalyzer(host=url, model="default", debug=False)
        self.assertEqual(ai.model, "Qwen3.8 Flash Next")
        self.assertEqual(ai.max_tokens, 262144)

    def test_unknown_server_stays_native(self):
        server, url = _make_server({})
        self.addCleanup(_stop_server, server)
        ai = LlmAnalyzer(host=url, model="test", debug=False)
        self.assertFalse(ai._openai_mode)
        self.assertEqual(ai.api_url, f"{url}/api/generate")
        self.assertEqual(ai.chat_api_url, f"{url}/api/chat")

    def test_explicit_v1_host_unchanged(self):
        server, url = _make_server({"/v1/models": (200, MODELS_PAYLOAD)})
        self.addCleanup(_stop_server, server)
        ai = LlmAnalyzer(host=f"{url}/v1", model="test", debug=False)
        self.assertTrue(ai._openai_mode)
        self.assertEqual(ai.api_url, f"{url}/v1/chat/completions")
        self.assertEqual(ai.chat_api_url, f"{url}/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
