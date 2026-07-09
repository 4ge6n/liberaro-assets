import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
import zlib


MODULE_PATH = Path(__file__).with_name("liberaro_upscale_server.py")
SPEC = importlib.util.spec_from_file_location("liberaro_upscale_server", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


def make_png(width=1, height=1):
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    compressed = zlib.compress(raw)

    def chunk(tag, body):
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


class LiberaroUpscaleServerAuthTests(unittest.TestCase):
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


class LiberaroUpscaleServerLimitTests(unittest.TestCase):
    def test_rejects_out_of_range_meta_values(self):
        with self.assertRaises(ValueError):
            server._validate_job_meta({"scale": server.MAX_SCALE + 1, "skipMinPixel": 0})
        with self.assertRaises(ValueError):
            server._validate_job_meta({"scale": 2, "skipMinPixel": server.MAX_SKIP_MIN_PIXEL + 1})

    def test_validates_uploaded_image_bytes_and_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.png"
            data = make_png(width=2, height=2)
            path.write_bytes(data)

            server._validate_uploaded_image(path, len(data))

            old_max = server.MAX_IMAGE_PIXELS
            try:
                server.MAX_IMAGE_PIXELS = 3
                with self.assertRaises(ValueError):
                    server._validate_uploaded_image(path, len(data))
            finally:
                server.MAX_IMAGE_PIXELS = old_max

    def test_rejects_oversized_uploaded_image_bytes(self):
        old_max = server.MAX_IMAGE_BYTES
        try:
            server.MAX_IMAGE_BYTES = 4
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "input.png"
                data = make_png()
                path.write_bytes(data)
                with self.assertRaises(ValueError):
                    server._validate_uploaded_image(path, len(data))
        finally:
            server.MAX_IMAGE_BYTES = old_max


class LiberaroUpscaleServerTileSizeTests(unittest.TestCase):
    def _job(self):
        return {
            "engine": "waifu2x",
            "model_id": "",
            "scale": 2,
            "noise": 1,
            "input_path": "/tmp/in.png",
            "output_path": "/tmp/out.png",
        }

    def test_tile_size_zero_omits_flag(self):
        old = server.TILE_SIZE
        try:
            server.TILE_SIZE = 0
            argv = server._build_waifu2x("/bin/waifu2x", self._job())
            self.assertNotIn("-t", argv)
        finally:
            server.TILE_SIZE = old

    def test_tile_size_passed_to_all_engines(self):
        old = server.TILE_SIZE
        try:
            server.TILE_SIZE = 256
            for builder in (
                server._build_waifu2x,
                server._build_real_cugan,
                server._build_real_esrgan,
            ):
                argv = builder("/bin/upscaler", self._job())
                idx = argv.index("-t")
                self.assertEqual(argv[idx + 1], "256")
        finally:
            server.TILE_SIZE = old


class LiberaroUpscaleServerRestoreTests(unittest.TestCase):
    def _write_job(self, root, job_id, status, with_input=True, cancel_requested=False):
        job_dir = root / job_id
        job_dir.mkdir(parents=True)
        if with_input:
            (job_dir / "input.bin").write_bytes(make_png())
        payload = {
            "id": job_id,
            "status": status,
            "error": None,
            "engine": "waifu2x",
            "model_id": "",
            "scale": 2,
            "noise": 1,
            "no_upscale": False,
            "skip_min_pixel": 0,
            "tmp_dir": str(job_dir),
            "input_path": str(job_dir / "input.bin"),
            "output_path": str(job_dir / "result.png"),
            "created_at": 100.0,
            "updated_at": 100.0,
            "finished_at": None,
            "cancel_requested": cancel_requested,
        }
        import json
        (job_dir / "job.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_restore_requeues_in_flight_jobs_with_input(self):
        old_root = server.JOB_ROOT
        old_jobs = dict(server._jobs)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                server.JOB_ROOT = root
                server._jobs.clear()

                self._write_job(root, "job-queued", "queued")
                self._write_job(root, "job-processing", "processing")
                self._write_job(root, "job-lost-input", "processing", with_input=False)
                self._write_job(root, "job-cancelling", "processing", cancel_requested=True)
                self._write_job(root, "job-done", "done")

                requeued = server._restore_jobs_from_disk()

                requeued_ids = sorted(j["id"] for j in requeued)
                self.assertEqual(requeued_ids, ["job-processing", "job-queued"])
                self.assertEqual(server._jobs["job-queued"]["status"], "queued")
                self.assertEqual(server._jobs["job-processing"]["status"], "queued")
                self.assertEqual(server._jobs["job-lost-input"]["status"], "failed")
                self.assertEqual(server._jobs["job-cancelling"]["status"], "cancelled")
                self.assertEqual(server._jobs["job-done"]["status"], "done")
        finally:
            server.JOB_ROOT = old_root
            server._jobs.clear()
            server._jobs.update(old_jobs)


class LiberaroUpscaleServerProgressTests(unittest.TestCase):
    def _job(self, status, created=None, finished=None):
        return {
            "status": status,
            "created_at": created,
            "finished_at": finished,
        }

    def test_progress_empty_when_no_jobs(self):
        old_jobs = dict(server._jobs)
        try:
            server._jobs.clear()
            snap = server._queue_progress_snapshot()
            self.assertEqual(snap["total"], 0)
            self.assertEqual(snap["remaining"], 0)
            self.assertIsNone(snap["avgSeconds"])
            self.assertIsNone(snap["etaSeconds"])
        finally:
            server._jobs.clear()
            server._jobs.update(old_jobs)

    def test_progress_counts_and_remaining(self):
        old_jobs = dict(server._jobs)
        try:
            server._jobs.clear()
            server._jobs["a"] = self._job("queued")
            server._jobs["b"] = self._job("processing")
            server._jobs["c"] = self._job("done", created=100.0, finished=110.0)
            server._jobs["d"] = self._job("failed")
            server._jobs["e"] = self._job("cancelled")

            snap = server._queue_progress_snapshot()
            self.assertEqual(snap["total"], 5)
            self.assertEqual(snap["queued"], 1)
            self.assertEqual(snap["processing"], 1)
            self.assertEqual(snap["done"], 1)
            self.assertEqual(snap["failed"], 1)
            self.assertEqual(snap["cancelled"], 1)
            self.assertEqual(snap["remaining"], 2)  # queued + processing
        finally:
            server._jobs.clear()
            server._jobs.update(old_jobs)

    def test_progress_eta_from_done_durations(self):
        old_jobs = dict(server._jobs)
        try:
            server._jobs.clear()
            # 2 件完了（各 10 秒）→ avg=10, remaining=2 → eta=20
            server._jobs["d1"] = self._job("done", created=0.0, finished=10.0)
            server._jobs["d2"] = self._job("done", created=100.0, finished=110.0)
            server._jobs["q1"] = self._job("queued")
            server._jobs["p1"] = self._job("processing")

            snap = server._queue_progress_snapshot()
            self.assertAlmostEqual(snap["avgSeconds"], 10.0)
            self.assertAlmostEqual(snap["etaSeconds"], 20.0)
        finally:
            server._jobs.clear()
            server._jobs.update(old_jobs)


if __name__ == "__main__":
    unittest.main()
