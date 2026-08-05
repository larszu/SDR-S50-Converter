#!/usr/bin/env python3
"""Headless tests for mod_convert_core — no ffmpeg required."""

import datetime
import os
import struct
import tempfile
import unittest
from fractions import Fraction

import mod_convert_core as core


class ProbeParsing(unittest.TestCase):
    def test_parse_probe_pal_anamorphic(self):
        text = (
            "width=720\nheight=576\nsample_aspect_ratio=64:45\n"
            "field_order=tt\nduration=8.640000\n"
        )
        # ffprobe reports 'tt'/'bb' too; our parser normalises unknowns to
        # progressive, so feed the canonical tokens it actually emits.
        text = text.replace("field_order=tt", "field_order=tff")
        info = core._parse_probe(text)
        self.assertEqual((info.width, info.height), (720, 576))
        self.assertEqual(info.sar, Fraction(64, 45))
        self.assertEqual(info.field_order, "tff")
        self.assertTrue(info.is_interlaced)
        self.assertAlmostEqual(info.duration, 8.64, places=2)

    def test_parse_ratio_fallbacks(self):
        self.assertEqual(core._parse_ratio("0:1"), Fraction(1, 1))
        self.assertEqual(core._parse_ratio("N/A"), Fraction(1, 1))
        self.assertEqual(core._parse_ratio(None), Fraction(1, 1))
        self.assertEqual(core._parse_ratio("16:9"), Fraction(16, 9))


class AspectMath(unittest.TestCase):
    def test_auto_pal_16_9(self):
        info = core.VideoInfo(width=720, height=576, sar=Fraction(64, 45))
        self.assertEqual(core.square_pixel_size(info, core.ASPECT_AUTO), (1024, 576))

    def test_override_16_9(self):
        info = core.VideoInfo(width=720, height=576, sar=Fraction(1, 1))
        self.assertEqual(core.square_pixel_size(info, "16:9"), (1024, 576))

    def test_override_4_3(self):
        info = core.VideoInfo(width=720, height=576, sar=Fraction(1, 1))
        self.assertEqual(core.square_pixel_size(info, "4:3"), (768, 576))

    def test_ntsc_auto(self):
        # 720x480 with 32:27 SAR -> ~853 wide (16:9), even.
        info = core.VideoInfo(width=720, height=480, sar=Fraction(32, 27))
        w, h = core.square_pixel_size(info, core.ASPECT_AUTO)
        self.assertEqual(h, 480)
        self.assertEqual(w % 2, 0)
        self.assertAlmostEqual(w, 854, delta=2)


class FilterAndCommand(unittest.TestCase):
    def setUp(self):
        self.info = core.VideoInfo(width=720, height=576,
                                   sar=Fraction(64, 45), field_order="tff",
                                   duration=8.64)

    def test_filter_50p(self):
        opts = core.ConvertOptions(mode=core.MODE_MP4)
        vf = core.build_video_filter(self.info, opts)
        self.assertIn("bwdif=mode=send_field:parity=tff", vf)
        self.assertIn("scale=1024:576:flags=lanczos", vf)
        self.assertTrue(vf.endswith("setsar=1"))

    def test_filter_25p(self):
        opts = core.ConvertOptions(mode=core.MODE_MP4_25P)
        vf = core.build_video_filter(self.info, opts)
        self.assertIn("mode=send_frame", vf)

    def test_filter_progressive_source_has_no_bwdif(self):
        prog = core.VideoInfo(width=720, height=576, sar=Fraction(64, 45),
                              field_order="progressive")
        vf = core.build_video_filter(prog, core.ConvertOptions())
        self.assertNotIn("bwdif", vf)

    def test_archive_has_no_filter(self):
        self.assertIsNone(
            core.build_video_filter(self.info, core.ConvertOptions(mode=core.MODE_ARCHIVE)))

    def test_command_mp4(self):
        opts = core.ConvertOptions(mode=core.MODE_MP4, crf=20)
        ct = datetime.datetime(2026, 5, 8, 14, 58, 49)
        cmd = core.build_command("MOV00F.MOD", self.info, opts, ct)
        self.assertIn("libx264", cmd)
        self.assertIn("-crf", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "20")
        self.assertIn("+faststart", cmd)
        self.assertIn("creation_time=2026-05-08T14:58:49", cmd)
        self.assertTrue(cmd[-1].endswith("MOV00F.mp4"))

    def test_command_archive_copies_streams(self):
        cmd = core.build_command("MOV00F.MOD", self.info,
                                 core.ConvertOptions(mode=core.MODE_ARCHIVE))
        self.assertIn("copy", cmd)
        self.assertIn("-map", cmd)
        self.assertTrue(cmd[-1].endswith("MOV00F.mkv"))


class MoiSidecar(unittest.TestCase):
    def test_read_datetime_and_pairing(self):
        with tempfile.TemporaryDirectory() as d:
            mod = os.path.join(d, "MOV00F.MOD")
            moi = os.path.join(d, "MOV00F.MOI")
            open(mod, "wb").close()
            # V6 magic + timestamp 2026-05-08 14:58:49 at offset 6.
            header = b"V6\x00\x00\x00\x00" + struct.pack(
                ">HBBBBB", 2026, 5, 8, 14, 58, 49)
            with open(moi, "wb") as fh:
                fh.write(header + b"\x00" * 200)

            self.assertEqual(core.moi_sidecar_path(mod), moi)
            dt = core.read_moi_datetime(moi)
            self.assertEqual(dt, datetime.datetime(2026, 5, 8, 14, 58, 49))

    def test_reject_implausible(self):
        with tempfile.NamedTemporaryFile(suffix=".MOI", delete=False) as fh:
            fh.write(b"V6\x00\x00\x00\x00" + struct.pack(">HBBBBB", 1900, 13, 40, 0, 0, 0))
            path = fh.name
        try:
            self.assertIsNone(core.read_moi_datetime(path))
        finally:
            os.remove(path)


class Discovery(unittest.TestCase):
    def test_discover_recursive_mixed_case(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "PRG001")
            os.makedirs(sub)
            for name in ("MOV00E.MOD", "MOV00F.mod", "clip.TOD", "notes.txt"):
                open(os.path.join(sub, name), "wb").close()
            found = core.discover_sources([d], recursive=True)
            self.assertEqual(len(found), 3)
            self.assertTrue(all(f.lower().endswith((".mod", ".tod")) for f in found))


if __name__ == "__main__":
    unittest.main(verbosity=2)
