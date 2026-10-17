"""Gateway revision drift: visible alert and explicit re-pin, against a fake gateway."""

from __future__ import annotations

import contextlib
import hashlib
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from tools.artifact_search import embedding_revision
from tools.artifact_search.embedding_revision import RevisionError, main, repin, semantic_check
from tools.artifact_search.runtime_vectors import SemanticConfig

OLD = "a" * 40
NEW = "c" * 40
MODEL_BYTES = b"GGUF synthetic model artifact"
MODEL = hashlib.sha256(MODEL_BYTES).hexdigest()
M5_KEY = "synthetic-m5-key-0123456789"
SEARCH_KEY = "synthetic-search-key-0123456789"
FLEET_TOKEN = "synthetic-fleet-token-0123456789"


def _keep_umask(test):
    """``main`` sets a private umask; restore it so later tests are unaffected."""
    previous = os.umask(0o022)
    os.umask(previous)
    test.addCleanup(os.umask, previous)


class FakeServices:
    """One loopback server playing the gateway, the search runtime and Heimdall."""

    def __init__(self):
        self.gateway_revision = NEW
        self.gateway_status = 200
        self.health = None
        self.health_status = 200
        self.panels = []
        self.requests = []
        services = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, code, value):
                data = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                services.requests.append(("GET", self.path, self.headers.get("Authorization")))
                if self.path == "/portal/me":
                    if services.gateway_status != 200:
                        self.send(services.gateway_status, {"error": "busy " + M5_KEY})
                        return
                    self.send(200, {"gateway": {"revision": services.gateway_revision}, "key": "x"})
                elif self.path == "/health" and services.health is not None:
                    self.send(services.health_status, services.health)
                else:
                    self.send(404, {"error": "not_found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                services.requests.append(("POST", self.path, self.headers.get("Authorization")))
                if self.path == "/panels":
                    services.panels.append(json.loads(body))
                    self.send(200, {"ok": True})
                else:
                    self.send(404, {"error": "not_found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def base(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def portal_calls(self):
        return [r for r in self.requests if r[1] == "/portal/me"]


def _health(enabled=True, revision=OLD + ":" + MODEL, vectors="complete", error=None):
    env = {"MIMIR_SEARCH_SEMANTIC": "on" if enabled else "off"}
    if enabled:
        env["MIMIR_SEARCH_EMBEDDING_REVISION"] = revision
    semantic = SemanticConfig.from_env(env).public()
    if error is not None:
        semantic = SemanticConfig(error=error).public()
    block = None
    if vectors == "complete":
        block = {"status": "complete", "identity": semantic.get("cache"), "units": 87, "covered": 87}
    elif vectors == "failed":
        block = {"status": "failed", "identity": semantic.get("cache"),
                 "error": "embedding_gateway_revision_mismatch"}
    elif vectors == "unavailable":
        block = {"status": "unavailable", "identity": semantic.get("cache"), "failures": 2,
                 "error": "embedding_transport_error", "retry_after": 10.0}
    elif vectors == "stale":
        block = {"status": "complete", "identity": "256d-0000000000000000", "units": 87, "covered": 87}
    semantic["vectors"] = block
    return {"status": "ready", "ready": True, "fresh": True, "semantic": semantic}


class RepinTests(unittest.TestCase):
    def setUp(self):
        _keep_umask(self)
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        os.chmod(self.root, 0o700)
        self.model = self.root / "embeddinggemma.gguf"
        self.model.write_bytes(MODEL_BYTES)
        self.env_file = self.root / "runtime.env"

    def tearDown(self):
        self.tempdir.cleanup()

    def _write_env(self, services, pin=None, extra=""):
        pin = pin if pin is not None else "MIMIR_SEARCH_EMBEDDING_REVISION=%s:%s\n" % (OLD, MODEL)
        text = ("# reader runtime\nMIMIR_SEARCH_API_KEY=%s\nMIMIR_SEARCH_SEMANTIC=on\n%s"
                "M5_OPENAI_BASE_URL=%s\nM5_API_KEY=%s\n%s" % (SEARCH_KEY, pin, services.base, M5_KEY, extra))
        self.env_file.write_text(text)
        os.chmod(self.env_file, 0o600)
        return self.env_file.read_bytes()

    def _run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["repin-embedding", "--env-file", str(self.env_file),
                         "--model-file", str(self.model), *args])
        for stream in (out.getvalue(), err.getvalue()):
            self.assertNotIn(M5_KEY, stream)
            self.assertNotIn(SEARCH_KEY, stream)
            self.assertNotIn("127.0.0.1", stream)
        return code, out.getvalue(), err.getvalue()

    def _backups(self):
        return sorted(p for p in self.root.iterdir() if ".pre-repin-" in p.name)

    def test_dry_run_reports_old_and_new_revision_without_writing(self):
        with FakeServices() as services:
            original = self._write_env(services)
            code, out, _ = self._run()
            self.assertEqual(code, 0)
            result = json.loads(out)
            self.assertEqual(result["old_revision"], OLD + ":" + MODEL)
            self.assertEqual(result["new_revision"], NEW + ":" + MODEL)
            self.assertTrue(result["changed"])
            self.assertFalse(result["applied"])
            self.assertTrue(result["model_sha256_verified"])
            self.assertEqual(self.env_file.read_bytes(), original)
            self.assertEqual(self._backups(), [])
            # The reader's own key, read in process, authenticates /portal/me.
            self.assertEqual(services.portal_calls(), [("GET", "/portal/me", "Bearer " + M5_KEY)])

    def test_apply_requires_the_revision_the_operator_saw(self):
        with FakeServices() as services:
            original = self._write_env(services)
            code, out, _ = self._run("--apply")
            self.assertEqual((code, json.loads(out)["error"]), (1, "expect_gateway_required"))
            code, out, _ = self._run("--apply", "--expect-gateway", "d" * 40)
            self.assertEqual((code, json.loads(out)["error"]), (1, "gateway_revision_unexpected"))
            self.assertEqual(self.env_file.read_bytes(), original)
            self.assertEqual(self._backups(), [])

    def test_apply_backs_up_and_rewrites_only_the_gateway_half_atomically(self):
        with FakeServices() as services:
            original = self._write_env(services)
            code, out, _ = self._run("--apply", "--expect-gateway", NEW)
            self.assertEqual(code, 0)
            result = json.loads(out)
            self.assertTrue(result["applied"])
            self.assertTrue(result["restart_required"])
            backups = self._backups()
            self.assertEqual(len(backups), 1)
            self.assertEqual(Path(result["backup"]), backups[0])
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
            expected = original.replace(("%s:%s" % (OLD, MODEL)).encode(), ("%s:%s" % (NEW, MODEL)).encode())
            self.assertEqual(self.env_file.read_bytes(), expected)
            self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])
            # A second run is a no-op: the pin is current.
            code, out, _ = self._run("--apply", "--expect-gateway", NEW)
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(out)["changed"])
            self.assertEqual(len(self._backups()), 1)

    def test_quoted_pin_keeps_its_quoting(self):
        with FakeServices() as services:
            self._write_env(services, pin='MIMIR_SEARCH_EMBEDDING_REVISION="%s:%s"\n' % (OLD, MODEL))
            code, _, _ = self._run("--apply", "--expect-gateway", NEW)
            self.assertEqual(code, 0)
            self.assertIn('MIMIR_SEARCH_EMBEDDING_REVISION="%s:%s"\n' % (NEW, MODEL), self.env_file.read_text())

    def test_model_change_is_refused_even_with_apply(self):
        with FakeServices() as services:
            original = self._write_env(services)
            self.model.write_bytes(MODEL_BYTES + b" retrained")
            code, out, _ = self._run("--apply", "--expect-gateway", NEW)
            self.assertEqual((code, json.loads(out)["error"]), (1, "model_sha256_mismatch"))
            self.assertEqual(self.env_file.read_bytes(), original)
            self.assertEqual(services.portal_calls(), [])

    def test_non_private_or_ambiguous_env_files_are_refused(self):
        with FakeServices() as services:
            self._write_env(services)
            os.chmod(self.env_file, 0o640)
            code, out, _ = self._run()
            self.assertEqual((code, json.loads(out)["error"]), (1, "private_env_file_required"))
            os.chmod(self.env_file, 0o600)
            link = self.root / "link.env"
            link.symlink_to(self.env_file)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["repin-embedding", "--env-file", str(link), "--model-file", str(self.model)])
            self.assertEqual((code, json.loads(out.getvalue())["error"]), (1, "private_env_file_required"))
            for pin, error in (("", "revision_pin_missing"),
                               ("MIMIR_SEARCH_EMBEDDING_REVISION=%s:%s\n" % (OLD, MODEL) * 2, "ambiguous_env_file"),
                               ("MIMIR_SEARCH_EMBEDDING_REVISION=%s\n" % OLD, "invalid_revision_pin")):
                with self.subTest(error=error):
                    original = self._write_env(services, pin=pin)
                    code, out, _ = self._run("--apply", "--expect-gateway", NEW)
                    self.assertEqual((code, json.loads(out)["error"]), (1, error))
                    self.assertEqual(self.env_file.read_bytes(), original)
            self.assertEqual(services.portal_calls(), [])

    def test_unusable_gateway_answers_never_change_the_pin(self):
        with FakeServices() as services:
            original = self._write_env(services)
            for revision, status, error in (("not-a-sha", 200, "embedding_gateway_revision_mismatch"),
                                            (NEW, 503, "embedding_http_error")):
                with self.subTest(error=error):
                    services.gateway_revision, services.gateway_status = revision, status
                    code, out, _ = self._run("--apply", "--expect-gateway", NEW)
                    self.assertEqual((code, json.loads(out)["error"]), (1, error))
                    self.assertEqual(self.env_file.read_bytes(), original)
            self.assertEqual(self._backups(), [])

    def test_public_route_is_refused_before_any_request(self):
        with FakeServices() as services:
            original = self._write_env(services)
            self.env_file.write_bytes(original.replace(services.base.encode(), b"https://gateway.example.com/v1"))
            code, out, _ = self._run()
            self.assertEqual((code, json.loads(out)["error"]), (1, "embedding_route_unavailable"))
            self.assertEqual(services.requests, [])

    def test_concurrent_edit_aborts_the_rewrite(self):
        with FakeServices() as services:
            self._write_env(services)
            real_backup = embedding_revision._backup

            def edit_then_backup(*args, **kwargs):
                path = real_backup(*args, **kwargs)
                with self.env_file.open("a") as stream:
                    stream.write("MIMIR_SEARCH_ABSTAIN_MIN_COSINE=0.5\n")
                return path

            with mock.patch.object(embedding_revision, "_backup", side_effect=edit_then_backup):
                code, out, _ = self._run("--apply", "--expect-gateway", NEW)
            self.assertEqual((code, json.loads(out)["error"]), (1, "env_file_changed"))
            text = self.env_file.read_text()
            self.assertIn("%s:%s" % (OLD, MODEL), text)
            self.assertIn("MIN_COSINE=0.5", text)
            self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])

    def test_packaged_ops_entry_routes_to_the_dry_run(self):
        from tools.artifact_search import operations
        with FakeServices() as services:
            self._write_env(services)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = operations.main(["repin-embedding", "--env-file", str(self.env_file),
                                        "--model-file", str(self.model)])
            self.assertEqual((code, json.loads(out.getvalue())["dry_run"]), (0, True))

    def test_library_call_refuses_without_explicit_expectation(self):
        with FakeServices() as services:
            self._write_env(services)
            with self.assertRaises(RevisionError):
                repin(self.env_file, self.model, apply=True)


class SemanticCheckTests(unittest.TestCase):
    def setUp(self):
        _keep_umask(self)

    def _env(self, services, heimdall=True, **overrides):
        env = {"MIMIR_SEARCH_API_KEY": SEARCH_KEY, "M5_OPENAI_BASE_URL": services.base, "M5_API_KEY": M5_KEY}
        if heimdall:
            env.update(HEIMDALL_HUB_URL="http://127.0.0.1:%d/panels" % services.port,
                       HEIMDALL_FLEET_TOKEN=FLEET_TOKEN)
        env.update(overrides)
        return env

    def _run(self, services, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["semantic-check", "--port", str(services.port)])
        for stream in (out.getvalue(), err.getvalue()):
            for secret in (M5_KEY, SEARCH_KEY, FLEET_TOKEN):
                self.assertNotIn(secret, stream)
        for panel in services.panels:
            self.assertNotIn(M5_KEY, json.dumps(panel))
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_simulated_revision_mismatch_raises_a_visible_alert(self):
        with FakeServices() as services:
            services.health = _health()  # runtime still pins OLD; vectors look complete
            code, result, _ = self._run(services, self._env(services))
            self.assertEqual(code, 1)
            self.assertEqual(result["state"], "fail")
            self.assertIn("embedding_gateway_revision_mismatch", result["codes"])
            self.assertTrue(result["pushed"])
            panel, = services.panels
            self.assertEqual((panel["service"], panel["panel"], panel["kind"], panel["state"]),
                             ("mimir", "search-semantic", "status", "fail"))
            self.assertIn("embedding_gateway_revision_mismatch", panel["message"])
            self.assertIn(("POST", "/panels", "Bearer " + FLEET_TOKEN), services.requests)
            self.assertIn(("GET", "/health", "Bearer " + SEARCH_KEY), services.requests)
            self.assertIn(("GET", "/portal/me", "Bearer " + M5_KEY), services.requests)

    def test_without_heimdall_a_mismatch_is_logged_loudly_and_fails_the_unit(self):
        with FakeServices() as services:
            services.health = _health()
            code, result, err = self._run(services, self._env(services, heimdall=False))
            self.assertEqual(code, 1)
            self.assertIsNone(result["pushed"])
            self.assertIn("ALERT", err)
            self.assertIn("embedding_gateway_revision_mismatch", err)

    def test_current_pin_is_a_pass(self):
        with FakeServices() as services:
            services.gateway_revision = OLD
            services.health = _health()
            code, result, err = self._run(services, self._env(services))
            self.assertEqual((code, result["state"]), (0, "pass"))
            self.assertEqual(set(result["codes"]), {"embedding_gateway_revision_current", "vectors_complete"})
            self.assertEqual(services.panels[0]["state"], "pass")
            self.assertEqual(err, "")

    def test_semantic_off_never_contacts_the_gateway(self):
        with FakeServices() as services:
            services.health = _health(enabled=False, vectors=None)
            code, result, _ = self._run(services, self._env(services))
            self.assertEqual((code, result["state"], result["codes"]), (0, "pass", ["semantic_disabled"]))
            self.assertEqual(services.portal_calls(), [])

    def test_runtime_semantic_error_and_vector_state_are_surfaced(self):
        cases = (
            (dict(error="invalid_embedding_revision", vectors=None), "fail", "invalid_embedding_revision"),
            (dict(vectors="failed"), "fail", "embedding_gateway_revision_mismatch"),
            (dict(vectors="unavailable"), "warn", "embedding_transport_error"),
            (dict(vectors=None), "warn", "vectors_not_started"),
            (dict(vectors="stale"), "warn", "vectors_identity_stale"),
        )
        for kwargs, state, code_name in cases:
            with self.subTest(code=code_name), FakeServices() as services:
                services.gateway_revision = OLD
                services.health = _health(**kwargs)
                code, result, _ = self._run(services, self._env(services))
                self.assertEqual(result["state"], state)
                self.assertIn(code_name, result["codes"])
                self.assertEqual(code, 1 if state == "fail" else 0)
                self.assertEqual(services.panels[0]["state"], state)

    def test_not_ready_runtime_still_reports_its_semantic_block(self):
        with FakeServices() as services:
            services.health, services.health_status = _health(), 503
            code, result, _ = self._run(services, self._env(services))
            self.assertIn("embedding_gateway_revision_mismatch", result["codes"])

    def test_unreachable_runtime_or_transient_gateway(self):
        with FakeServices() as services:
            code, result, _ = self._run(services, self._env(services))  # /health 404
            self.assertEqual((code, result["state"], result["codes"]), (1, "fail", ["runtime_health_unavailable"]))
        with FakeServices() as services:
            services.health, services.gateway_status = _health(), 503
            code, result, _ = self._run(services, self._env(services))
            self.assertEqual((code, result["state"]), (0, "warn"))
            self.assertIn("embedding_unavailable", result["codes"])

    def test_missing_route_credentials_fail_without_network(self):
        with FakeServices() as services:
            services.health = _health()
            env = self._env(services)
            del env["M5_API_KEY"]
            code, result, _ = self._run(services, env)
            self.assertEqual(result["state"], "fail")
            self.assertIn("embedding_route_unavailable", result["codes"])
            self.assertEqual(services.portal_calls(), [])

    def test_check_function_is_pure_of_side_effects(self):
        with FakeServices() as services:
            services.health = _health()
            result = semantic_check(self._env(services), port=services.port)
            self.assertEqual(result["state"], "fail")
            self.assertEqual(services.panels, [])


class IdentityTests(unittest.TestCase):
    def test_model_or_gateway_change_gives_a_new_cache_identity(self):
        base = SemanticConfig.from_env({"MIMIR_SEARCH_SEMANTIC": "on",
                                        "MIMIR_SEARCH_EMBEDDING_REVISION": OLD + ":" + MODEL}).tag
        for revision in (NEW + ":" + MODEL, OLD + ":" + "e" * 64):
            with self.subTest(revision=revision):
                other = SemanticConfig.from_env({"MIMIR_SEARCH_SEMANTIC": "on",
                                                 "MIMIR_SEARCH_EMBEDDING_REVISION": revision}).tag
                self.assertNotEqual(base, other)


if __name__ == "__main__":
    unittest.main()
