from __future__ import annotations

import unittest

from jobbot.crawl_state import ExhaustionTracker


class ExhaustionTrackerTests(unittest.TestCase):
    def test_explicit_end_and_age_boundary_are_exhausted(self) -> None:
        tracker = ExhaustionTracker()
        self.assertEqual(tracker.observe("a", 0, explicit_end=True)[0], "exhausted")
        self.assertEqual(tracker.observe("b", 0, age_boundary=True)[0], "exhausted")

    def test_safety_guards_never_claim_exhaustion(self) -> None:
        tracker = ExhaustionTracker(max_identical_fingerprints=3)
        tracker.observe("same", 2); tracker.observe("same", 0)
        status, reason = tracker.observe("same", 0)
        self.assertEqual(status, "incomplete")
        self.assertIn("SAFETY_STOP", reason)


if __name__ == "__main__": unittest.main()
