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
    def test_flash_line_is_top_right_with_style(self):
        out = osd.flash_line(3, "#33CC33", 36)
        self.assertIn("{\\an9}", out)
        self.assertIn("{\\fnVCR OSD Mono}", out)
        self.assertIn("{\\fs36}", out)
        self.assertIn("{\\c&H33CC33&}", out)
        self.assertIn("{\\bord3}", out)
        self.assertTrue(out.endswith("CH 03"))

    def test_info_block_is_centred_with_hard_breaks(self):
        out = osd.info_block(3, "X", "Title", "5m 00s", "#33CC33", 36)
        self.assertIn("{\\an8}", out)
        self.assertIn("{\\fnVCR OSD Mono}", out)
        self.assertIn("{\\fs36}", out)
        self.assertIn("{\\c&H33CC33&}", out)
        self.assertIn("{\\bord3}", out)
        # The three overlay lines stay one event: hard \N breaks, not \n.
        self.assertNotIn("\n", out)
        self.assertIn("\\N", out)
        self.assertIn("CH 03 · ", out)
        self.assertIn("5m 00s remaining", out)

    def test_user_controlled_text_is_ass_escaped(self):
        out = osd.info_block(3, "We{ird}", "Ti{tle}", "5m 00s", "#33CC33", 36)
        self.assertIn("We\\{ird\\}", out)
        self.assertIn("Ti\\{tle\\}", out)

    def test_ass_escape_escapes_special_chars(self):
        self.assertEqual(osd.ass_escape("a{b}c\\d"), "a\\{b\\}c\\\\d")

    def test_flash_over_info_has_both_anchors_on_separate_events(self):
        out = osd.compose_osd(
            osd.flash_line(3, "#33CC33", 36),
            osd.info_block(3, "X", "Title", "5m 00s", "#33CC33", 36),
        )
        self.assertIn("{\\an9}", out)
        self.assertIn("{\\an8}", out)
        lines = out.split("\n")
        # The flash is its own event; the three overlay lines share the second.
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith("CH 03"))
        self.assertTrue(lines[1].startswith("{\\an8}"))
        self.assertIn("CH 03 · X", lines[1])
        self.assertIn("Title", lines[1])
        self.assertIn("5m 00s remaining", lines[1])

    def test_ass_color_converts_rgb_to_bgr(self):
        self.assertEqual(osd.ass_color("#FF0000"), "&H0000FF&")  # red -> BGR swap
        self.assertEqual(osd.ass_color("#0000FF"), "&HFF0000&")  # blue -> BGR swap
        self.assertEqual(osd.ass_color("#123456"), "&H563412&")
        self.assertEqual(osd.ass_color("#33CC33"), "&H33CC33&")  # palindromic

    def test_ass_color_bad_input_falls_back_to_default_green(self):
        for bad in ("red", "#12345", "", 42, None, "#GGHHII"):
            self.assertEqual(osd.ass_color(bad), "&H33CC33&")

    def test_font_size_out_of_range_falls_back(self):
        self.assertIn("{\\fs36}", osd.flash_line(3, "#33CC33", 4))
        self.assertIn("{\\fs36}", osd.flash_line(3, "#33CC33", "big"))
        self.assertIn("{\\fs48}", osd.flash_line(3, "#33CC33", 48))


class IconAssWrappersTest(unittest.TestCase):
    def test_volume_line_unmuted_icon_and_level(self):
        out = osd.volume_line(55, False, "#33CC33", 36)
        self.assertIn("{\\an1}", out)
        self.assertIn("{\\fnpixelart-icons-font}", out)
        self.assertIn(osd.ICON_VOLUME, out)
        self.assertIn("{\\fnVCR OSD Mono} 55", out)

    def test_volume_line_muted_shows_crossed_icon_and_level(self):
        out = osd.volume_line(0, True, "#33CC33", 36)
        self.assertIn(osd.ICON_MUTED, out)
        self.assertNotIn(osd.ICON_VOLUME, out)
        self.assertIn("{\\fnVCR OSD Mono} 0", out)  # level still shown

    def test_volume_line_is_anchored(self):
        out = osd.volume_line(30, False, "#33CC33", 36)
        self.assertTrue(out.startswith("{\\an1}"))

    def test_flash_line_is_top_right(self):
        out = osd.flash_line(3, "#33CC33", 36)
        self.assertIn("{\\an9}", out)
        self.assertTrue(out.endswith("CH 03"))

    def test_compose_osd_joins_events_with_newlines(self):
        out = osd.compose_osd("{\\an9}A", "{\\an1}B")
        self.assertEqual(out, "{\\an9}A\n{\\an1}B")


class SleepLineTest(unittest.TestCase):
    def test_chooser_brackets_selected_option(self):
        out = osd.sleep_chooser_line(["30m", "60m", "90m", "Off"], 1, "#33CC33", 36)
        self.assertIn("[60m]", out)
        self.assertNotIn("[30m]", out)
        self.assertIn("{\\an2}", out)

    def test_flash_line_is_bottom_center(self):
        out = osd.sleep_flash_line("Sleep 60m", "#33CC33", 36)
        self.assertIn("{\\an2}", out)
        self.assertIn("Sleep 60m", out)

    def test_countdown_rounds_up(self):
        self.assertIn("Off in 1:00", osd.sleep_countdown_line(59.9, "#33CC33", 36))
        self.assertIn("Off in 0:45", osd.sleep_countdown_line(45.0, "#33CC33", 36))
        self.assertIn("Off in 0:00", osd.sleep_countdown_line(0.0, "#33CC33", 36))


if __name__ == "__main__":
    unittest.main()
