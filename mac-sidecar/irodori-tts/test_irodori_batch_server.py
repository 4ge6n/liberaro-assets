import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).with_name("irodori_batch_server.py")
SPEC = importlib.util.spec_from_file_location("irodori_batch_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class IrodoriBatchServerAuthTests(unittest.TestCase):
    def test_requires_expected_token(self):
        server._configure_auth(token="secret")

        self.assertFalse(server._is_authorized_headers({}))
        self.assertFalse(server._is_authorized_headers({"Authorization": "Bearer wrong"}))
        self.assertTrue(server._is_authorized_headers({"Authorization": "Bearer secret"}))
        self.assertTrue(server._is_authorized_headers({"X-Liberaro-Token": "secret"}))

    def test_generates_and_reuses_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "server_token.txt"

            first = server._configure_auth(token_file=str(token_file))
            second = server._configure_auth(token_file=str(token_file))

            self.assertTrue(token_file.exists())
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)


class IrodoriBatchServerGradioAllowlistTests(unittest.TestCase):
    def test_allows_loopback_default_gradio_ports(self):
        self.assertEqual(
            server._normalize_gradio_base_url("http://127.0.0.1:7860/"),
            "http://127.0.0.1:7860",
        )
        self.assertEqual(
            server._normalize_gradio_base_url("http://localhost:7861"),
            "http://localhost:7861",
        )

    def test_rejects_non_allowlisted_gradio_targets(self):
        for url in [
            "http://127.0.0.1:22",
            "http://169.254.169.254:7860",
            "http://192.168.1.10:7860",
            "file:///tmp/socket",
            "http://127.0.0.1:7860/admin",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    server._normalize_gradio_base_url(url)

    def test_rejects_non_allowlisted_paths(self):
        with self.assertRaises(ValueError):
            server._validate_gradio_path("run_path", "/admin", server.ALLOWED_RUN_PATHS)
        with self.assertRaises(ValueError):
            server._validate_gradio_path(
                "run_path",
                "http://127.0.0.1:7860/gradio_api/run/_run_generation",
                server.ALLOWED_RUN_PATHS,
            )

    def test_rejects_cross_origin_audio_urls(self):
        base = server._normalize_gradio_base_url("http://127.0.0.1:7860")

        with self.assertRaises(ValueError):
            server._resolve_audio_url(
                base,
                "/gradio_api/file=",
                {"url": "http://169.254.169.254/latest.wav"},
            )

        self.assertEqual(
            server._resolve_audio_url(
                base,
                "/gradio_api/file=",
                {"url": "http://127.0.0.1:7860/gradio_api/file=/tmp/a.wav"},
            ),
            "http://127.0.0.1:7860/gradio_api/file=/tmp/a.wav",
        )


class IrodoriBatchServerRetentionTests(unittest.TestCase):
    def setUp(self):
        self._old_job_root = server.JOB_ROOT
        self._old_retention = server.JOB_RETENTION_SECONDS
        self.tmp = tempfile.TemporaryDirectory()
        server.JOB_ROOT = Path(self.tmp.name)
        server.JOB_ROOT.mkdir(parents=True, exist_ok=True)
        with server._jobs_lock:
            server._jobs.clear()

    def tearDown(self):
        with server._jobs_lock:
            server._jobs.clear()
        server.JOB_ROOT = self._old_job_root
        server.JOB_RETENTION_SECONDS = self._old_retention
        self.tmp.cleanup()

    def test_new_job_does_not_persist_sensitive_payload(self):
        job = server._new_job(
            {
                "chunks": [{"chapter_index": 0, "chunk_index": 0, "text": "本文"}],
                "config": {"reference_audio_base64": "secret-audio"},
            }
        )
        job_dir = Path(job["tmp_dir"])

        self.assertTrue((job_dir / "job.json").exists())
        self.assertFalse((job_dir / "payload.json").exists())

    def test_prunes_finished_job_dirs_after_restart(self):
        server.JOB_RETENTION_SECONDS = 10
        job_dir = server.JOB_ROOT / "stale-job"
        job_dir.mkdir()
        (job_dir / "job.json").write_text(
            """
            {
              "id": "stale-job",
              "status": "completed",
              "total": 1,
              "completed_chunks": [],
              "failed_chunks": [],
              "tmp_dir": "unused",
              "created_at": 0,
              "updated_at": 1,
              "finished_at": 1
            }
            """,
            encoding="utf-8",
        )

        server._prune_finished_job_dirs_on_disk(now=20)

        self.assertFalse(job_dir.exists())


if __name__ == "__main__":
    unittest.main()
