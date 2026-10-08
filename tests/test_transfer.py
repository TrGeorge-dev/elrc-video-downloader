"""Offline fault-injection tests for ELRC transfer core; no campus requests."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest

import requests

import transfer


def box(name, content):
    return struct.pack(">I4s", len(content) + 8, name) + content


VIDEO = box(b"ftyp", b"isom\0\0\0\0isom") + box(b"moov", box(b"free", b"x" * 24)) + box(b"mdat", b"x" * (1024 * 1024))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        state = self.server.state
        state["requests"].append(dict(self.headers))
        mode = state["mode"]
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://localhost:1/forbidden")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "html":
            body = b"<html>login</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        start = 0
        ranged = self.headers.get("Range") and mode != "ignore"
        if ranged:
            start = int(self.headers["Range"].removeprefix("bytes=").removesuffix("-"))
        if start >= len(VIDEO):
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = VIDEO[start:]
        self.send_response(206 if ranged else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"changed"' if mode == "changed" else '"fixture-v1"')
        if ranged:
            first = start + 1 if mode == "wrong_range" else start
            self.send_header("Content-Range", f"bytes {first}-{len(VIDEO)-1}/{len(VIDEO)}")
        self.end_headers()
        if mode == "truncated":
            body = body[:600000]
            self.close_connection = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/video.mp4?private=do-not-save"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.target = Path(self.folder.name) / "中文 空格" / "课程.mp4"
        self.server.state = {"mode": "normal", "requests": []}
        self.cancel = threading.Event()
        self.logs = []
        self.updates = []
        self.session = requests.Session()
        self.session.trust_env = False

    def tearDown(self):
        self.session.close()
        self.folder.cleanup()

    def download(self, progress=None):
        return transfer.download_media(self.session, self.url, self.target, {}, self.cancel,
                                       progress or (lambda a, b: self.updates.append((a, b))), self.logs.append)

    def seed(self, size=123456, validator=True):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        part = self.target.with_name(self.target.name + ".part")
        part.write_bytes(VIDEO[:size])
        self.target.with_name(self.target.name + ".part.json").write_text(json.dumps({
            "source_sha256": hashlib.sha256(self.url.encode()).hexdigest(),
            "etag": '"fixture-v1"' if validator else None,
            "expected_size": len(VIDEO),
        }))
        return part

    def test_complete_and_existing_verified(self):
        self.assertEqual(self.download(), self.target)
        self.assertEqual(self.target.read_bytes(), VIDEO)
        self.assertEqual(self.updates[-1], (len(VIDEO), len(VIDEO)))
        self.download()
        self.assertEqual(len(self.server.state["requests"]), 1)
        metadata = self.target.with_name(self.target.name + ".elrc.json").read_text()
        self.assertNotIn("private", metadata)
        self.assertNotIn(self.url, "".join(self.logs))

    def test_resume_is_byte_exact(self):
        self.seed()
        self.download()
        self.assertEqual(self.target.read_bytes(), VIDEO)
        sent = self.server.state["requests"][0]
        self.assertEqual(sent["Range"], "bytes=123456-")
        self.assertEqual(sent["If-Range"], '"fixture-v1"')

    def test_range_ignored_restarts(self):
        self.seed()
        self.server.state["mode"] = "ignore"
        self.download()
        self.assertEqual(self.target.read_bytes(), VIDEO)

    def test_fully_received_part_can_be_finalized(self):
        self.seed(size=len(VIDEO))
        self.download()
        self.assertEqual(self.target.read_bytes(), VIDEO)
        self.assertFalse(self.server.state["requests"])

    def test_partial_without_validator_restarts(self):
        self.seed(validator=False)
        self.download()
        self.assertEqual(self.target.read_bytes(), VIDEO)
        self.assertNotIn("Range", self.server.state["requests"][0])

    def test_wrong_range_preserves_partial(self):
        part = self.seed()
        self.server.state["mode"] = "wrong_range"
        with self.assertRaises(transfer.DownloadError):
            self.download()
        self.assertEqual(part.read_bytes(), VIDEO[:123456])
        self.assertFalse(self.target.exists())

    def test_changed_version_preserves_partial(self):
        part = self.seed()
        self.server.state["mode"] = "changed"
        with self.assertRaises(transfer.DownloadError):
            self.download()
        self.assertEqual(part.read_bytes(), VIDEO[:123456])
        self.assertFalse(self.target.exists())

    def test_html_is_not_a_video(self):
        self.server.state["mode"] = "html"
        with self.assertRaises(transfer.DownloadError):
            self.download()
        self.assertFalse(self.target.exists())

    def test_short_connection_does_not_publish(self):
        self.server.state["mode"] = "truncated"
        with self.assertRaises(transfer.DownloadError):
            self.download()
        self.assertFalse(self.target.exists())
        self.server.state["mode"] = "normal"
        self.download()
        self.assertEqual(self.target.read_bytes(), VIDEO)

    def test_unknown_final_is_preserved(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"existing")
        with self.assertRaises(transfer.DownloadError):
            self.download()
        self.assertEqual(self.target.read_bytes(), b"existing")
        self.assertFalse(self.server.state["requests"])

    def test_other_source_partial_is_preserved(self):
        part = self.seed()
        self.url += "&another=1"
        with self.assertRaises(transfer.DownloadError):
            self.download()
        self.assertEqual(part.read_bytes(), VIDEO[:123456])

    def test_cancel_and_resume(self):
        def progress(done, total):
            if done >= 262144:
                self.cancel.set()
        with self.assertRaises(transfer.Cancelled):
            self.download(progress)
        self.assertFalse(self.target.exists())
        self.cancel.clear()
        self.download()
        self.assertEqual(self.target.read_bytes(), VIDEO)

    def test_cancel_at_last_byte_can_be_finalized(self):
        def progress(done, total):
            if total and done == total:
                self.cancel.set()
        with self.assertRaises(transfer.Cancelled):
            self.download(progress)
        self.assertFalse(self.target.exists())
        self.cancel.clear()
        self.server.state["requests"].clear()
        self.download()
        self.assertEqual(self.target.read_bytes(), VIDEO)
        self.assertFalse(self.server.state["requests"])

    def test_cross_host_redirect_blocked(self):
        self.server.state["mode"] = "redirect"
        with self.assertRaises(transfer.DownloadError):
            self.download()
        self.assertEqual(len(self.server.state["requests"]), 1)

    def test_mp4_boundaries(self):
        self.target.parent.mkdir(parents=True)
        for content in (VIDEO[:-1], VIDEO + b"x", b"<html>login</html>"):
            with self.subTest(size=len(content)):
                self.target.write_bytes(content)
                with self.assertRaises(transfer.DownloadError):
                    transfer.validate_mp4(self.target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
