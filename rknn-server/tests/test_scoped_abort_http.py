"""Exercise the deployed Flask handler without loading the RKLLM runtime."""
import ast
from pathlib import Path
import threading
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inference_cancellation import InferenceCancellation
from flask import Flask, jsonify, request


class ScopedAbortHttpTests(unittest.TestCase):
    def setUp(self):
        self.guard = InferenceCancellation()
        ready = threading.Event()
        ready.set()
        path = Path(__file__).resolve().parents[1] / "llm_server.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        handler = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.FunctionDef) and node.name == "abort_inference")
        handler.decorator_list = []
        namespace = dict(_model_ready=ready, _call_cancellation=self.guard,
                         jsonify=jsonify, request=request, print=lambda *a, **k: None)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), str(path), "exec"), namespace)
        app = Flask(__name__)
        app.add_url_rule('/rkllm_abort', view_func=namespace['abort_inference'], methods=['POST'])
        self.client = app.test_client()

    def test_owned_close_cancels_active_and_rejects_queued_request(self):
        self.guard.start("chat", "old")
        result = self.client.post('/rkllm_abort', json={'conversation_id': 'old', 'close_session': True})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['aborted'])
        self.assertTrue(self.guard.cancelled.is_set())
        self.guard.finish()
        self.assertFalse(self.guard.start("chat", "old"))

    def test_delayed_close_leaves_new_model_owner_running(self):
        self.guard.start("chat", "new")
        result = self.client.post('/rkllm_abort', json={'conversation_id': 'old', 'close_session': True})
        self.assertFalse(result.json['aborted'])
        self.assertFalse(self.guard.cancelled.is_set())

    def test_close_without_identity_is_not_global_abort(self):
        self.guard.start("chat", "new")
        result = self.client.post('/rkllm_abort', json={'close_session': True})
        self.assertEqual(result.status_code, 400)
        self.assertFalse(self.guard.cancelled.is_set())

    def test_explicit_legacy_abort_is_compatible(self):
        self.guard.start("chat", "new")
        result = self.client.post('/rkllm_abort', json={})
        self.assertTrue(result.json['aborted'])
