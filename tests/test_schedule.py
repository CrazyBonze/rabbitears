"""Unit tests for rabbitears.schedule (clock-based position math)."""

import unittest

from rabbitears.schedule import compute_position, find_file

EPOCH = 1_000_000.0


class ComputePositionTest(unittest.TestCase):
    def test_basic_position_within_loop(self):
        # 500s after epoch in a 1000s loop → position 500.
        self.assertEqual(compute_position(EPOCH, 1000.0, EPOCH + 500.0), 500.0)

    def test_position_at_epoch_is_zero(self):
        self.assertEqual(compute_position(EPOCH, 1000.0, EPOCH), 0.0)

    def test_position_at_exact_loop_boundary_wraps_to_zero(self):
        # Exactly one full loop later → back to 0.
        self.assertEqual(compute_position(EPOCH, 1000.0, EPOCH + 1000.0), 0.0)

    def test_position_wraps_past_one_loop(self):
        # 2.75 loops in → position 750.
        self.assertEqual(compute_position(EPOCH, 1000.0, EPOCH + 2750.0), 750.0)

    def test_result_is_always_in_range(self):
        for offset in (0.0, 1.0, 999.999, 1000.0, 2500.0, 10000.0):
            pos = compute_position(EPOCH, 1000.0, EPOCH + offset)
            self.assertGreaterEqual(pos, 0.0)
            self.assertLess(pos, 1000.0)

    def test_now_before_epoch_wraps_to_end_of_loop(self):
        # Chosen behavior: Python's modulo keeps the result in
        # [0, loop_length), so a clock set before the epoch lands near
        # the *end* of the loop (1000 - 300 = 700) instead of crashing.
        # SCOPE §4.2: a dead CMOS battery shifts the schedule; it must
        # not break anything.
        self.assertEqual(compute_position(EPOCH, 1000.0, EPOCH - 300.0), 700.0)

    def test_zero_loop_length_raises(self):
        with self.assertRaises(ValueError):
            compute_position(EPOCH, 0.0, EPOCH)


class FindFileTest(unittest.TestCase):
    FILES = [
        ("a.mp4", 100.0),
        ("b.mp4", 200.0),
        ("c.mp4", 300.0),
    ]

    def test_basic_position_within_single_file(self):
        self.assertEqual(find_file(self.FILES, 42.0), ("a.mp4", 42.0))

    def test_position_at_file_start(self):
        self.assertEqual(find_file(self.FILES, 0.0), ("a.mp4", 0.0))

    def test_position_spanning_multiple_files(self):
        # 100 + 150 → 150s into b.mp4.
        self.assertEqual(find_file(self.FILES, 250.0), ("b.mp4", 150.0))
        # 100 + 200 + 50 → 50s into c.mp4.
        self.assertEqual(find_file(self.FILES, 350.0), ("c.mp4", 50.0))

    def test_position_at_exact_file_boundary(self):
        # Exactly at the end of a.mp4 → start of b.mp4.
        self.assertEqual(find_file(self.FILES, 100.0), ("b.mp4", 0.0))

    def test_position_at_exact_loop_boundary_wraps_to_first_file(self):
        # Total length 600 → back to the start of the loop.
        self.assertEqual(find_file(self.FILES, 600.0), ("a.mp4", 0.0))

    def test_empty_files_raises_value_error(self):
        with self.assertRaises(ValueError):
            find_file([], 0.0)

    def test_negative_position_raises(self):
        with self.assertRaises(ValueError):
            find_file(self.FILES, -1.0)


if __name__ == "__main__":
    unittest.main()
