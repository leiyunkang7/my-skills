import argparse
import http.server
import importlib.util
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

api = load("clash_api")
observer = load("clash_observe")


class Handler(http.server.BaseHTTPRequestHandler):
    calls = []
    def log_message(self, *args):
        pass
    def do_GET(self):
        self.calls.append((self.path, self.headers.get("Authorization")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/leak")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"version":"fixture-core"}')


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.cert = Path(cls.temp.name) / "ca.pem"
        cls.key = Path(cls.temp.name) / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
                        "-subj", "/CN=fixture", "-addext", "subjectAltName=IP:127.0.0.1"],
                       check=True, capture_output=True)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, cls.key)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"https://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def setUp(self):
        Handler.calls.clear()
        api.configure_transport(argparse.Namespace(ca_cert=str(self.cert), read_only=True))

    def test_tls_and_environment_proxy_are_separate(self):
        with patch.dict(os.environ, {"https_proxy": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1"}):
            status, payload = api.request_json(self.url, "fixture-secret", "GET", "version")
        self.assertEqual((status, payload["version"]), (200, "fixture-core"))
        self.assertEqual(Handler.calls, [("/version", "Bearer fixture-secret")])

    def test_untrusted_ca_and_hostname_mismatch(self):
        api.CA_CERT = None
        with self.assertRaises(api.ControllerUnreachable):
            api.request_json(self.url, "", "GET", "version")
        api.CA_CERT = str(self.cert)
        with self.assertRaises(api.ControllerUnreachable):
            api.request_json(self.url.replace("127.0.0.1", "localhost"), "", "GET", "version")

    def test_redirect_never_forwards_secret(self):
        status, body = api.request_json(self.url, "fixture-secret", "GET", "redirect")
        self.assertEqual((status, body), (302, None))
        self.assertEqual(len(Handler.calls), 1)

    def test_mutation_rejected_before_network(self):
        with self.assertRaises(api.OpRejected):
            api.request_json(self.url, "", "PATCH", "configs", payload={"mode": "global"})
        self.assertEqual(Handler.calls, [])

    def test_origin_validation(self):
        self.assertEqual(api.strip_scheme("127.0.0.1:9090"), "http://127.0.0.1:9090")
        for value in ("https://u:p@localhost:9090", "https://localhost:9090/?secret=x",
                      "https://localhost:9090/a", "ftp://localhost:9090", "https://localhost:bad"):
            with self.assertRaises(api.ClashError):
                api.strip_scheme(value)

    def test_explicit_target_never_discovers_another(self):
        args = argparse.Namespace(controller=self.url, secret="fixture-secret")
        with patch.object(api, "_tv_controller", side_effect=AssertionError("discovery called")):
            self.assertEqual(api.discover(args)[0], self.url)

    def test_remote_egress_needs_explicit_proxy(self):
        with self.assertRaises(api.ClashError):
            api._default_proxy("https://192.0.2.1:9090", "")

    def test_private_secret_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            secret = Path(directory) / "secret"
            secret.write_text("fixture-secret\n")
            secret.chmod(0o600)
            self.assertEqual(api.read_secret_file(str(secret)), "fixture-secret")
            secret.chmod(0o644)
            with self.assertRaises(api.ClashError):
                api.read_secret_file(str(secret))
            secret.chmod(0o600)
            link = Path(directory) / "alias"
            link.symlink_to(secret)
            with self.assertRaises(api.ClashError):
                api.read_secret_file(str(link))
            link.unlink()
            os.link(secret, link)
            with self.assertRaises(api.ClashError):
                api.read_secret_file(str(secret))


class ObservationTests(unittest.TestCase):
    def test_log_filter_is_exact_and_source_scoped(self):
        msg = "[TCP] 192.0.2.2:1234 --> api.example.com:443 match DomainSuffix(example.com) using AI[fixture]"
        self.assertIsNotNone(observer.parse_log(msg, "api.example.com", "192.0.2.2"))
        self.assertIsNone(observer.parse_log(msg, "example.com", None))
        self.assertIsNone(observer.parse_log(msg, "api.example.com", "192.0.2.3"))
        self.assertIsNone(observer.parse_log("secret=do-not-retain", "api.example.com", None))

    def test_missing_evidence_is_not_bypass(self):
        def fake_request(host, secret, method, endpoint, **kwargs):
            return 200, {"version": "fixture"} if endpoint == "version" else {"connections": []}
        with patch.object(api, "request_json", side_effect=fake_request), \
             patch.object(api, "controller_open", side_effect=OSError("unavailable")):
            result = observer.observe(api, "http://127.0.0.1:9090", "", "example.com", duration=1)
        self.assertEqual(result["evidence"], "insufficient")
        self.assertIn("log-stream-unavailable-or-idle", result["collection_warnings"])


if __name__ == "__main__":
    unittest.main()
