"""
Tests for the pure (non-terminal) logic in decisions-panel.py: the fold reuse,
sort order, change detection, and selection movement including wrap-around.

The rendering and key loop can't be unit-tested end to end (they need a real
Windows console for msvcrt) — those are covered by a manual live run instead
(see the report handed back with this file). This file covers everything
that CAN be exercised headlessly.

Run with: python -m pytest test_decisions_panel.py -v
"""
import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# decisions-panel.py has a hyphen, so it can't be `import`ed by name — load it
# by path instead. This also confirms the file itself is syntactically valid
# and importable, independent of Windows-only bits (msvcrt import is guarded).
_spec = importlib.util.spec_from_file_location(
    "decisions_panel", os.path.join(os.path.dirname(os.path.abspath(__file__)), "decisions-panel.py")
)
panel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(panel)

import decisions as dec  # noqa: E402


class StatKeyTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".log")
        os.close(fd)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_missing_file_is_none(self):
        os.remove(self.path)
        self.assertIsNone(panel.stat_key(self.path))

    def test_stat_changes_after_append(self):
        before = panel.stat_key(self.path)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("hello\n")
        after = panel.stat_key(self.path)
        self.assertTrue(panel.has_changed(before, after))

    def test_stat_unchanged_when_nothing_written(self):
        a = panel.stat_key(self.path)
        b = panel.stat_key(self.path)
        self.assertFalse(panel.has_changed(a, b))


class SortOpenItemsTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.log_path = tempfile.mkstemp(suffix=".log")
        os.close(fd)
        os.remove(self.log_path)

    def tearDown(self):
        for p in (self.log_path, self.log_path + ".lock"):
            if os.path.exists(p):
                os.remove(p)

    def test_oldest_opened_sorts_first(self):
        dec.cmd_open(self.log_path, "b-key", question="second", asked_by="x")
        dec.cmd_open(self.log_path, "a-key", question="first opened, waiting longest", asked_by="y")
        # a-key was opened after b-key here, so to test "oldest first" for
        # real we need actual timestamp order, not insertion order — rely on
        # ts field directly instead of wall-clock sleeps.
        events, _ = dec.read_events(self.log_path)
        states = dec.fold_state(events)
        # Force a controlled ordering via explicit opened_at values.
        states["a-key"].opened_at = "2020-01-01T00:00:00+00:00"
        states["b-key"].opened_at = "2020-01-02T00:00:00+00:00"
        ordered = panel.sort_open_items(states)
        self.assertEqual([s.key for s in ordered], ["a-key", "b-key"])

    def test_closed_items_excluded(self):
        dec.cmd_open(self.log_path, "open-one", question="q", asked_by="x")
        dec.cmd_open(self.log_path, "closed-one", question="q2", asked_by="x")
        dec.cmd_close(self.log_path, "closed-one", answer="done")
        events, _ = dec.read_events(self.log_path)
        states = dec.fold_state(events)
        ordered = panel.sort_open_items(states)
        self.assertEqual([s.key for s in ordered], ["open-one"])


class MoveSelectionTestCase(unittest.TestCase):
    def test_wraps_past_end_going_down(self):
        self.assertEqual(panel.move_selection(2, 1, 3), 0)

    def test_wraps_past_start_going_up(self):
        self.assertEqual(panel.move_selection(0, -1, 3), 2)

    def test_simple_move_within_bounds(self):
        self.assertEqual(panel.move_selection(0, 1, 3), 1)
        self.assertEqual(panel.move_selection(1, -1, 3), 0)

    def test_empty_list_always_zero(self):
        self.assertEqual(panel.move_selection(0, 1, 0), 0)
        self.assertEqual(panel.move_selection(5, -1, 0), 0)

    def test_single_item_stays_put(self):
        self.assertEqual(panel.move_selection(0, 1, 1), 0)
        self.assertEqual(panel.move_selection(0, -1, 1), 0)


class ClampSelectionTestCase(unittest.TestCase):
    def test_clamps_to_last_index_when_shrunk(self):
        self.assertEqual(panel.clamp_selection(5, 3), 2)

    def test_leaves_in_range_selection_untouched(self):
        self.assertEqual(panel.clamp_selection(1, 3), 1)

    def test_empty_list_is_zero(self):
        self.assertEqual(panel.clamp_selection(4, 0), 0)


class RenderNoLogFileTestCase(unittest.TestCase):
    def test_render_list_reports_no_open_decisions_when_log_missing(self):
        missing_path = os.path.join(tempfile.gettempdir(), "definitely-does-not-exist-panel-test.log")
        if os.path.exists(missing_path):
            os.remove(missing_path)
        text = panel.render_list([], 0, 0, missing_path)
        self.assertIn("no open decisions", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
