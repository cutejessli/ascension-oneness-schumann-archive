import unittest

from PIL import Image, ImageDraw

from tomsk_archive_utils import append_with_overlap, find_best_overlap, rebuild_stitched_timeline


def make_canonical_chart(width: int = 520, height: int = 140) -> Image.Image:
    """Create a chart-like strip with static framing and changing content rows."""

    image = Image.new("RGB", (width, height), "#030712")
    draw = ImageDraw.Draw(image)

    # Static frame/grid: these must not dictate the overlap choice.
    draw.rectangle((0, 0, width - 1, height - 1), outline="#737d90")
    for y in (10, 30, 50, 70, 90, 110, 130):
        draw.line((0, y, width, y), fill="#182237")

    # The central band varies horizontally like a sonogram.
    for x in range(width):
        phase = (x * 17 + x * x * 3) % 97
        for y in range(20, 112):
            value = (phase + y * 11 + (x // 7) * 13) % 255
            if value > 168:
                draw.point((x, y), fill=(32, value, 255 - value // 3))
            elif value > 128:
                draw.point((x, y), fill=(30, value, 118))
    return image


class StitchingTests(unittest.TestCase):
    def test_one_day_advance_prefers_two_day_overlap(self) -> None:
        # Each image is 300px wide (three days). Moving the source forward by
        # 100px is one day, leaving 200px / 66.7% as true overlap.
        canonical = make_canonical_chart()
        first = canonical.crop((0, 0, 300, canonical.height))
        second = canonical.crop((100, 0, 400, canonical.height))

        result = find_best_overlap(first, second)

        self.assertGreaterEqual(result["overlap_percent"], 63)
        self.assertLessEqual(result["overlap_percent"], 70)
        self.assertGreaterEqual(result["append_percent"], 30)
        self.assertLessEqual(result["append_percent"], 37)
        self.assertNotIn("implausible_daily_append_width", result["warnings"])

    def test_rebuild_appends_about_one_day_without_a_border(self) -> None:
        canonical = make_canonical_chart()
        first = canonical.crop((0, 0, 300, canonical.height))
        second = canonical.crop((100, 0, 400, canonical.height))
        third = canonical.crop((200, 0, 500, canonical.height))

        timeline, steps = rebuild_stitched_timeline([
            ("2026-07-05", first),
            ("2026-07-06", second),
            ("2026-07-07", third),
        ])

        self.assertIsNotNone(timeline)
        self.assertGreaterEqual(timeline.width, 480)
        self.assertLessEqual(timeline.width, 520)
        self.assertEqual(steps[1]["resulting_timeline_width_px"], timeline.width - steps[2]["append_width_px"])
        self.assertGreaterEqual(steps[2]["append_percent"], 30)
        self.assertLessEqual(steps[2]["append_percent"], 37)

    def test_no_tiny_append_is_selected_from_an_implausible_candidate(self) -> None:
        canonical = make_canonical_chart()
        first = canonical.crop((0, 0, 300, canonical.height))
        second = canonical.crop((100, 0, 400, canonical.height))

        _, result = append_with_overlap(first, second)

        self.assertGreaterEqual(result["append_percent"], 28)
        self.assertGreaterEqual(result["append_width_px"], 84)


if __name__ == "__main__":
    unittest.main()
