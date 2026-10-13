import unittest

from job_utils import public_job_snapshot


class JobApiSerializationTests(unittest.TestCase):
    def test_snapshot_excludes_internal_callable_and_output_locks(self):
        job = {
            "id": "test-job",
            "status": "running",
            "log": [{"phase": "upscale", "message": "Using Lanczos"}],
            "themes_total": 1,
            "themes_done": 0,
            "runner": lambda: None,
            "lock_reals": ["/private/theme/path"],
        }
        snapshot = public_job_snapshot(job)

        self.assertEqual(snapshot["id"], "test-job")
        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["log"][0]["message"], "Using Lanczos")
        self.assertNotIn("runner", snapshot)
        self.assertNotIn("lock_reals", snapshot)

    def test_snapshot_is_detached_from_live_job_mutations(self):
        job = {"id": "test-job", "log": [{"message": "initial"}]}
        snapshot = public_job_snapshot(job)
        job["log"][0]["message"] = "updated"
        self.assertEqual(snapshot["log"][0]["message"], "initial")


if __name__ == "__main__":
    unittest.main()
