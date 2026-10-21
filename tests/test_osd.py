"""Unit tests for rabbitears.osd (pure OSD text formatting, SCOPE §4.5)."""

import unittest
from pathlib import Path

from rabbitears import osd


class ChannelFlashTextTest(unittest.TestCase):
    def test_zero_padded(self):
        self.assertEqual(osd.channel_flash_text(1), "CH 01")

    def test_two_digit(self):
        self.assertEqual(osd.channel_flash_text(42), "CH 42")

    def test_no_name_nothing_else(self):
        self.assertEqual(osd.channel_flash_text(3), "CH 03")


class ShowTitleTest(unittest.TestCase):
    def test_strips_extension(self):
        self.assertEqual(osd.show_title(Path("foo.mp4")), "foo")

    def test_real_filename(self):
        self.assertEqual(
            osd.show_title(Path("S01E01 - Opening.mp4")), "S01E01 - Opening"
        )

    def test_no_extension_unchanged(self):
        self.assertEqual(osd.show_title(Path("foo")), "foo")


class RemainingSecondsTest(unittest.TestCase):
    def test_exact_arithmetic(self):
        self.assertEqual(osd.remaining_seconds(100.0, 20.0, 30.0), 50.0)

    def test_clamps_at_zero_for_overrun(self):
        self.assertEqual(osd.remaining_seconds(100.0, 20.0, 90.0), 0.0)

    def test_at_elapsed_zero(self):
        self.assertEqual(osd.remaining_seconds(60.0, 15.0, 0.0), 45.0)


class FormatRemainingTest(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(osd.format_remaining(0), "0m 00s")
        self.assertEqual(osd.format_remaining(59), "0m 59s")
        self.assertEqual(osd.format_remaining(60), "1m 00s")
        self.assertEqual(osd.format_remaining(61), "1m 01s")
        self.assertEqual(osd.format_remaining(3600), "1h 00m 00s")
        self.assertEqual(osd.format_remaining(3661), "1h 01m 01s")

    def test_fractional_truncated(self):
        self.assertEqual(osd.format_remaining(1230.9), "20m 30s")


class InfoTextTest(unittest.TestCase):
    def test_three_lines_in_order(self):
        text = osd.info_text(3, "Magic School Bus", "S01E01 - Opening", "12m 30s")
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0], "CH 03 · Magic School Bus")
        self.assertEqual(lines[1], "S01E01 - Opening")
        self.assertEqual(lines[2], "12m 30s remaining")


class AssWrappersTest(unittest.TestCase):
    UNESC = "\u0092a0"  # mpv's show-text unescape marker (osd-ass-cc/0)
    ESC = "\u0092a1"    # ...and the re-escape marker (osd-ass-cc/1)

    def test_flash_is_top_right_with_style(self):
        out = osd.flash_ass("CH 03", "#33CC33", 72)
        self.assertTrue(out.startswith(self.UNESC))
        self.assertIn("{\\an9}", out)
        self.assertIn("{\\fnVCR OSD Mono}", out)
        self.assertIn("{\\fs72}", out)
        self.assertIn("{\\c&H33CC33&}", out)
        self.assertIn("{\\bord3}", out)
        self.assertTrue(out.endswith("CH 03"))

    def test_info_is_centred_with_style(self):
        out = osd.info_ass(3, "X", "Title", "5m 00s", "#33CC33", 36)
        self.assertTrue(out.startswith(self.UNESC))
        self.assertIn("{\\an8}", out)
        self.assertIn("{\\fnVCR OSD Mono}", out)
        self.assertIn("{\\fs36}", out)
        self.assertIn("{\\c&H33CC33&}", out)
        self.assertIn("{\\bord3}", out)
        lines = out.split("\n")
        self.assertEqual(len(lines), 3)
        self.assertIn("CH 03 · ", lines[0])
        self.assertIn("5m 00s remaining", lines[2])

    def test_user_controlled_text_is_re_escaped(self):
        out = osd.info_ass(3, "We{ird}", "Ti{tle}", "5m 00s", "#33CC33", 36)
        # The escape marker precedes the name and the title, so a "{" in a
        # filename can't start an ASS tag.
        self.assertIn(self.ESC + "We{ird}", out)
        self.assertIn(self.ESC + "Ti{tle}", out)

    def test_flash_over_info_has_both_anchors_on_separate_lines(self):
        out = osd.flash_over_info_ass(3, "X", "Title", "5m 00s",
                                      "#33CC33", 72, 36)
        self.assertTrue(out.startswith(self.UNESC))
        self.assertIn("{\\an9}", out)
        self.assertIn("{\\an8}", out)
        self.assertIn("{\\fs72}", out)
        self.assertIn("{\\fs36}", out)
        lines = out.split("\n")
        # The flash is its own line (ASS anchors position lines, not spans),
        # above the three overlay lines.
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].endswith("CH 03"))
        self.assertTrue(lines[1].startswith("{\\an8}"))
        self.assertTrue(lines[1].endswith(self.ESC + "X"))  # name re-escaped
        self.assertEqual(lines[2], self.ESC + "Title")      # title re-escaped
        self.assertEqual(lines[3], "5m 00s remaining")

    def test_ass_color_converts_rgb_to_bgr(self):
        self.assertEqual(osd.ass_color("#FF0000"), "&H0000FF&")  # red -> BGR swap
        self.assertEqual(osd.ass_color("#0000FF"), "&HFF0000&")  # blue -> BGR swap
        self.assertEqual(osd.ass_color("#123456"), "&H563412&")
        self.assertEqual(osd.ass_color("#33CC33"), "&H33CC33&")  # palindromic

    def test_ass_color_bad_input_falls_back_to_default_green(self):
        for bad in ("red", "#12345", "", 42, None, "#GGHHII"):
            self.assertEqual(osd.ass_color(bad), "&H33CC33&")

    def test_font_size_out_of_range_falls_back(self):
        self.assertIn("{\\fs72}", osd.flash_ass("CH 03", "#33CC33", 4))
        self.assertIn("{\\fs72}", osd.flash_ass("CH 03", "#33CC33", "big"))
        self.assertIn("{\\fs48}", osd.flash_ass("CH 03", "#33CC33", 48))


if __name__ == "__main__":
    unittest.main()
