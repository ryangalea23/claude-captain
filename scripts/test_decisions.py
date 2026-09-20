"""
Tests for decisions.py — the append-only open-decisions ledger.

Run with:  python -m pytest test_decisions.py -v
(or just:  python test_decisions.py   — falls back to
a plain unittest runner if pytest isn't installed, see bottom of file)
"""
import os
import sys
import threading
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decisions  # noqa: E402


class DecisionsTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        os.remove(self.log_path)  # start with a file that doesn't exist yet

    def tearDown(self):
        if os.path.exists(self.log_path):
            os.remove(self.log_path)
        lock_path = self.log_path + ".lock"
        if os.path.exists(lock_path):
            os.remove(lock_path)

    # ---- core fold behaviour ----

    def test_open_then_buried_under_50_lines_still_shows_open(self):
        ok, _ = decisions.cmd_open(
            self.log_path, "gpu-quota", question="Can we raise the quota?",
            asked_by="firstmate-eval",
        )
        self.assertTrue(ok)

        for i in range(50):
            decisions.cmd_open(
                self.log_path, f"noise-{i}", question="filler", asked_by="x"
            )

        events, malformed = decisions.read_events(self.log_path)
        self.assertEqual(malformed, 0)
        state = decisions.fold_state(events)
        self.assertIn("gpu-quota", state)
        self.assertEqual(state["gpu-quota"].status, "open")

    def test_close_with_wrong_key_does_not_close_the_open_one(self):
        decisions.cmd_open(self.log_path, "real-key", question="q", asked_by="a")
        ok, msg = decisions.cmd_close(self.log_path, "wrong-key", answer="yes")
        self.assertFalse(ok)
        self.assertIn("wrong-key", msg)

        events, _ = decisions.read_events(self.log_path)
        state = decisions.fold_state(events)
        self.assertEqual(state["real-key"].status, "open")
        self.assertNotIn("wrong-key", state)

    def test_malformed_line_in_the_middle_does_not_break_the_fold(self):
        decisions.cmd_open(self.log_path, "before", question="q1", asked_by="a")
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write("{this is not valid json\n")
        decisions.cmd_open(self.log_path, "after", question="q2", asked_by="b")

        events, malformed = decisions.read_events(self.log_path)
        self.assertEqual(malformed, 1)
        state = decisions.fold_state(events)
        self.assertEqual(state["before"].status, "open")
        self.assertEqual(state["after"].status, "open")

    def test_reopening_a_closed_key_works_and_shows_open_again(self):
        decisions.cmd_open(self.log_path, "k1", question="q", asked_by="a")
        ok, _ = decisions.cmd_close(self.log_path, "k1", answer="done")
        self.assertTrue(ok)

        events, _ = decisions.read_events(self.log_path)
        state = decisions.fold_state(events)
        self.assertEqual(state["k1"].status, "closed")

        ok, msg = decisions.cmd_open(self.log_path, "k1", question="q2", asked_by="a")
        self.assertTrue(ok, msg)

        events, _ = decisions.read_events(self.log_path)
        state = decisions.fold_state(events)
        self.assertEqual(state["k1"].status, "open")
        self.assertEqual(state["k1"].question, "q2")

    def test_cannot_reuse_a_key_that_is_currently_open(self):
        decisions.cmd_open(self.log_path, "dup", question="q", asked_by="a")
        ok, msg = decisions.cmd_open(self.log_path, "dup", question="q2", asked_by="b")
        self.assertFalse(ok)
        self.assertIn("dup", msg)

    def test_cannot_close_a_key_that_was_never_opened(self):
        ok, msg = decisions.cmd_close(self.log_path, "never", answer="x")
        self.assertFalse(ok)
        self.assertIn("never", msg)

    def test_cannot_close_a_key_that_is_already_closed(self):
        decisions.cmd_open(self.log_path, "k2", question="q", asked_by="a")
        decisions.cmd_close(self.log_path, "k2", answer="ans")
        ok, msg = decisions.cmd_close(self.log_path, "k2", answer="ans2")
        self.assertFalse(ok)
        self.assertIn("k2", msg)

    # ---- concurrency ----

    def test_two_concurrent_appends_both_survive_intact(self):
        results = []

        def worker(n):
            ok, _ = decisions.cmd_open(
                self.log_path, f"concurrent-{n}", question=f"q{n}", asked_by="t"
            )
            results.append(ok)

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertTrue(all(results))
        events, malformed = decisions.read_events(self.log_path)
        self.assertEqual(malformed, 0)
        state = decisions.fold_state(events)
        self.assertIn("concurrent-1", state)
        self.assertIn("concurrent-2", state)

    def test_twenty_concurrent_writers_all_land(self):
        """High-contention regression test for the Windows O_APPEND race:
        the CRT emulates append as seek-then-write, so without a real lock
        two writers can seek to the same offset and one write clobbers the
        other — both calls still report success, but a line goes missing.
        Reproduced directly: two threads on distinct keys lost a write in
        about 1 of 4 trials before the file lock was added. This raises the
        contention to 20 writers and demands ALL 20 land, every time."""
        n = 20
        results = [None] * n

        def worker(i):
            ok, _ = decisions.cmd_open(
                self.log_path, f"stress-{i}", question=f"q{i}", asked_by="t"
            )
            results[i] = ok

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(results), results)
        events, malformed = decisions.read_events(self.log_path)
        self.assertEqual(malformed, 0)
        self.assertEqual(len(events), n, "a write went missing under contention")
        state = decisions.fold_state(events)
        for i in range(n):
            self.assertIn(f"stress-{i}", state)
            self.assertEqual(state[f"stress-{i}"].status, "open")

    # ---- list/show plumbing ----

    def test_list_json_reports_unreadable_line_count(self):
        decisions.cmd_open(self.log_path, "a", question="q", asked_by="x")
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write("garbage\n")
        out = decisions.render_list(self.log_path, as_json=True)
        import json
        payload = json.loads(out)
        self.assertEqual(payload["unreadable_lines"], 1)
        self.assertEqual(len(payload["open"]), 1)

    def test_show_returns_full_history_for_one_key(self):
        decisions.cmd_open(self.log_path, "hist", question="q1", asked_by="a")
        decisions.cmd_close(self.log_path, "hist", answer="ans1")
        decisions.cmd_open(self.log_path, "hist", question="q2", asked_by="a")
        out = decisions.render_show(self.log_path, "hist")
        self.assertIn("q1", out)
        self.assertIn("ans1", out)
        self.assertIn("q2", out)


if __name__ == "__main__":
    unittest.main()
