"""Shared fixtures: a scriptable mock of Ollama (/api/show, /api/chat, /api/embed) and llama-server
(/v1/chat/completions), with streaming, so nothing here needs a GPU or a model."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "agent-jo", ROOT / "bench"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

FIXTURE = ROOT / "bench" / "fixture" / "FX"
BRAIN = None


def real_brain() -> str:
    global BRAIN
    if BRAIN is None:
        import jo_powerbi
        BRAIN = jo_powerbi.brain_from_modelfile(str(ROOT / "Modelfile"))
    return BRAIN


class MockLLM:
    """script: list of assistant messages (dicts) or callables(body) -> message, consumed per chat call."""

    def __init__(self):
        self.script, self.requests = [], []
        self.system = "BRAIN: Power BI only."
        self.capabilities = ["completion", "tools", "thinking"]
        self.default = None

    def next_message(self, body):
        if self.script:
            m = self.script.pop(0)
        elif self.default is not None:
            m = self.default
        else:
            raise AssertionError("mock script exhausted")
        return m(body) if callable(m) else m


def _chunks(text, n=7):
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


def make_server(mock: MockLLM):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _stream(self, lines, ctype):
            data = "".join(lines).encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            mock.requests.append((self.path, body))
            if self.path == "/api/show":
                return self._send({"system": mock.system, "capabilities": mock.capabilities})
            if self.path == "/api/embed":
                vecs = [[float(len(t) % 7), float(("dax" in t.lower())), 1.0] for t in body["input"]]
                return self._send({"embeddings": vecs})
            if self.path == "/api/chat":
                m = mock.next_message(body)
                if not body.get("stream"):
                    return self._send({"message": m, "eval_count": 100, "eval_duration": 1_000_000_000})
                lines = []
                for part in _chunks(m.get("thinking", "")) if m.get("thinking") else []:
                    lines.append(json.dumps({"message": {"role": "assistant", "content": "", "thinking": part},
                                             "done": False}) + "\n")
                for part in _chunks(m.get("content", "")):
                    lines.append(json.dumps({"message": {"role": "assistant", "content": part}, "done": False}) + "\n")
                if m.get("tool_calls"):
                    lines.append(json.dumps({"message": {"role": "assistant", "content": "",
                                                         "tool_calls": m["tool_calls"]}, "done": False}) + "\n")
                lines.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}) + "\n")
                return self._stream(lines, "application/x-ndjson")
            if self.path == "/v1/chat/completions":
                m = mock.next_message(body)
                if not body.get("stream"):
                    return self._send({"choices": [{"message": m}]})
                lines = []
                for part in _chunks(m.get("content") or ""):
                    lines.append("data: " + json.dumps({"choices": [{"delta": {"content": part}}]}) + "\n\n")
                for idx, tc in enumerate(m.get("tool_calls") or []):
                    args = tc["function"]["arguments"]
                    args = args if isinstance(args, str) else json.dumps(args)
                    first = {"index": idx, "id": tc.get("id", f"call_{idx}"),
                             "function": {"name": tc["function"]["name"], "arguments": ""}}
                    lines.append("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [first]}}]}) + "\n\n")
                    for part in _chunks(args, 5):
                        frag = {"index": idx, "function": {"arguments": part}}
                        lines.append("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [frag]}}]}) + "\n\n")
                lines.append("data: [DONE]\n\n")
                return self._stream(lines, "text/event-stream")
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def llm():
    mock = MockLLM()
    srv = make_server(mock)
    mock.url = f"http://127.0.0.1:{srv.server_port}"
    yield mock
    srv.shutdown()


@pytest.fixture
def sandbox(tmp_path):
    import shutil
    dst = tmp_path / "PBI" / "FX"
    shutil.copytree(FIXTURE, dst)
    return dst


def final(text, thinking=""):
    m = {"role": "assistant", "content": text}
    if thinking:
        m["thinking"] = thinking
    return m


def call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}
