import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentcal.browser.profile import ProfileBusy, ProfileLease

ROOT = Path(__file__).resolve().parents[1]


class ProfileTest(unittest.TestCase):
    def test_lock_is_exclusive_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            holder = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys,time;from agentcal.browser.profile import ProfileLease;"
                        "l=ProfileLease(sys.argv[1], wait_s=1);l.acquire();print('HOLD',flush=True);"
                        "time.sleep(3)"
                    ),
                    tmp,
                ],
                cwd=str(ROOT),
                env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
                stdout=subprocess.PIPE,
                text=True,
            )
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "HOLD")
            try:
                with self.assertRaises(ProfileBusy):
                    ProfileLease(tmp, wait_s=0.2, poll_s=0.02).acquire()
            finally:
                holder.kill()
                holder.wait(timeout=5)
                if holder.stdout is not None:
                    holder.stdout.close()

    def test_lock_can_be_reacquired_after_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            lease = ProfileLease(tmp, wait_s=1)
            lease.acquire()
            lease.release()
            with ProfileLease(tmp, wait_s=1):
                pass
