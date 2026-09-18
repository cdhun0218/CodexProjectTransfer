"""Optional read-only Codex protocol smoke test. No login or model calls."""
import json
import os
import queue
import shutil
import subprocess
import threading
import unittest

from test_transfer import TransferTests
from transfer_core import restore_bundle


@unittest.skipUnless(shutil.which("codex"), "Codex CLI is not installed")
class CodexIntegration(unittest.TestCase):
    def test_codex_reads_restored_session(self):
        fixture = TransferTests()
        fixture.setUp()
        try:
            event = {"timestamp": "2026-09-17T00:00:00Z", "type": "session_meta", "payload": {
                "id": fixture.sid, "timestamp": "2026-09-17T00:00:00Z", "cwd": str(fixture.project),
                "originator": "codex_cli_rs", "cli_version": "0.114.0", "source": "cli", "model_provider": "openai"}}
            item = {"timestamp": "2026-09-17T00:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "transfer fixture hello"}]}}
            fixture.rollout.write_text(json.dumps(event) + "\n" + json.dumps(item) + "\n", encoding="utf-8")
            (fixture.home / "state_5.sqlite").unlink()
            fixture.export()
            target = fixture.root / "restored"
            restore_bundle(fixture.package, target)
            env = os.environ.copy(); env["CODEX_HOME"] = str(target / "codex")
            proc = subprocess.Popen([shutil.which("codex"), "app-server", "--stdio"], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                    encoding="utf-8", env=env, cwd=target / "project")
            messages = queue.Queue()
            def reader():
                for line in proc.stdout:
                    try: messages.put(json.loads(line))
                    except ValueError: pass
            thread = threading.Thread(target=reader, daemon=True); thread.start()
            def call(identifier, method, params):
                proc.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
                proc.stdin.flush()
                while True:
                    result = messages.get(timeout=20)
                    if result.get("id") == identifier: return result
            try:
                result = call(1, "initialize", {"clientInfo": {"name": "transfer-test", "version": "0.1"}, "capabilities": {"experimentalApi": True}})
                self.assertIn("result", result)
                proc.stdin.write('{"method":"initialized"}\n'); proc.stdin.flush()
                result = call(2, "thread/read", {"threadId": fixture.sid, "includeTurns": True})
                self.assertNotIn("error", result, result)
                self.assertEqual(result["result"]["thread"]["id"], fixture.sid)
                self.assertEqual(result["result"]["thread"]["cwd"], str(target / "project"))
            finally:
                proc.terminate(); proc.wait(timeout=10); thread.join(timeout=5)
                proc.stdin.close(); proc.stdout.close()
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
