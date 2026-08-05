#!/usr/bin/env python3
# SDR-S50 Converter — convert Panasonic/JVC camcorder .MOD/.TOD files
# Copyright (C) 2026  larszu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
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
        # Shaped like the real ffprobe output for MOV00E.MOD.
        text = (
            "codec_type=video\ncodec_name=mpeg2video\n"
            "width=720\nheight=576\nsample_aspect_ratio=64:45\n"
            "field_order=tt\nr_frame_rate=25/1\ncolor_space=bt470bg\n"
            "codec_type=audio\ncodec_name=mp2\nsample_rate=48000\nchannels=2\n"
            "format_name=mpeg\nduration=8.640000\n"
        )
        info = core._parse_probe(text)
        self.assertEqual(info.codec, "mpeg2video")
        self.assertEqual(info.audio_codec, "mp2")
        self.assertEqual(info.color_space, "bt470bg")
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


class FieldOrderAndContainer(unittest.TestCase):
    """The two controls added after analysing a real SDR-S50 clip."""

    def setUp(self):
        self.info = core.VideoInfo(width=720, height=576, sar=Fraction(64, 45),
                                   field_order="tff", duration=4.8)

    def test_probe_normalises_ffprobe_tt_to_tff(self):
        info = core._parse_probe(
            "codec_type=video\nwidth=720\nheight=576\n"
            "sample_aspect_ratio=64:45\nfield_order=tt\n")
        self.assertEqual(info.field_order, "tff")

    def test_probe_normalises_bb_to_bff(self):
        info = core._parse_probe("codec_type=video\nfield_order=bb\n")
        self.assertEqual(info.field_order, "bff")

    def test_auto_uses_detected_order(self):
        opts = core.ConvertOptions(field_order=core.FIELD_AUTO)
        self.assertEqual(core.effective_parity(self.info, opts), "tff")

    def test_override_forces_bff(self):
        opts = core.ConvertOptions(field_order=core.FIELD_BFF)
        self.assertEqual(core.effective_parity(self.info, opts), "bff")
        self.assertIn("parity=bff", core.build_video_filter(self.info, opts))

    def test_forced_order_deinterlaces_progressive_flagged_source(self):
        prog = core.VideoInfo(width=720, height=576, sar=Fraction(64, 45),
                              field_order="progressive")
        opts = core.ConvertOptions(field_order=core.FIELD_TFF)
        self.assertTrue(core.should_deinterlace(prog, opts))
        self.assertIn("bwdif", core.build_video_filter(prog, opts))

    def test_archive_container_selects_extension(self):
        mkv = core.ConvertOptions(mode=core.MODE_ARCHIVE,
                                  archive_container=core.CONTAINER_MKV)
        mp4 = core.ConvertOptions(mode=core.MODE_ARCHIVE,
                                  archive_container=core.CONTAINER_MP4)
        self.assertTrue(core.output_path_for("A.MOD", mkv).endswith("A.mkv"))
        self.assertTrue(core.output_path_for("A.MOD", mp4).endswith("A.mp4"))

    def test_archive_mp4_still_copies_streams(self):
        opts = core.ConvertOptions(mode=core.MODE_ARCHIVE,
                                   archive_container=core.CONTAINER_MP4)
        cmd = core.build_command("A.MOD", self.info, opts)
        self.assertIn("copy", cmd)
        self.assertNotIn("libx264", cmd)
        self.assertTrue(cmd[-1].endswith("A.mp4"))


class ColourPreservation(unittest.TestCase):
    def test_pal_colour_tags_are_carried_over(self):
        info = core.VideoInfo(width=720, height=576, sar=Fraction(64, 45),
                              field_order="tff", color_space="bt470bg",
                              color_primaries="bt470bg", color_trc="bt470bg",
                              color_range="tv")
        cmd = core.build_command("A.MOD", info, core.ConvertOptions())
        self.assertIn("-colorspace", cmd)
        self.assertEqual(cmd[cmd.index("-colorspace") + 1], "bt470bg")
        self.assertEqual(cmd[cmd.index("-color_primaries") + 1], "bt470bg")

    def test_unknown_colour_is_not_tagged(self):
        info = core.VideoInfo(width=720, height=576, field_order="tff")
        cmd = core.build_command("A.MOD", info, core.ConvertOptions())
        self.assertNotIn("-colorspace", cmd)

    def test_dar_derived_from_sar(self):
        info = core.VideoInfo(width=720, height=576, sar=Fraction(64, 45))
        self.assertEqual(info.dar, "16:9")


class MoiSidecar(unittest.TestCase):
    def test_seconds_come_from_millisecond_field(self):
        """Byte 12 is not seconds: 0xBF68 = 49000 ms = second 49."""
        with tempfile.TemporaryDirectory() as d:
            moi = os.path.join(d, "MOV00E.MOI")
            with open(moi, "wb") as fh:
                fh.write(b"V6\x00\x00\x01\x2b"
                         + bytes([0x07, 0xEA, 0x05, 0x08, 0x0E, 0x3A, 0xBF, 0x68])
                         + b"\x00" * 200)
            self.assertEqual(core.read_moi_datetime(moi),
                             datetime.datetime(2026, 5, 8, 14, 58, 49))

    def test_reject_out_of_range_milliseconds(self):
        with tempfile.TemporaryDirectory() as d:
            moi = os.path.join(d, "X.MOI")
            with open(moi, "wb") as fh:
                fh.write(b"V6\x00\x00\x00\x00"
                         + bytes([0x07, 0xEA, 0x05, 0x08, 0x0E, 0x3A, 0xFF, 0xFF])
                         + b"\x00" * 200)
            self.assertIsNone(core.read_moi_datetime(moi))

    def test_read_datetime_and_pairing(self):
        with tempfile.TemporaryDirectory() as d:
            mod = os.path.join(d, "MOV00F.MOD")
            moi = os.path.join(d, "MOV00F.MOI")
            open(mod, "wb").close()
            # V6 magic + timestamp 2026-05-08 14:58:49 at offset 6.
            header = b"V6\x00\x00\x00\x00" + struct.pack(
                ">HBBBBH", 2026, 5, 8, 14, 58, 49_000)
            with open(moi, "wb") as fh:
                fh.write(header + b"\x00" * 200)

            self.assertEqual(core.moi_sidecar_path(mod), moi)
            dt = core.read_moi_datetime(moi)
            self.assertEqual(dt, datetime.datetime(2026, 5, 8, 14, 58, 49))

    def test_reject_implausible(self):
        with tempfile.NamedTemporaryFile(suffix=".MOI", delete=False) as fh:
            fh.write(b"V6\x00\x00\x00\x00" + struct.pack(">HBBBBH", 1900, 13, 40, 0, 0, 0))
            path = fh.name
        try:
            self.assertIsNone(core.read_moi_datetime(path))
        finally:
            os.remove(path)


class ToolResolution(unittest.TestCase):
    """The bundled-ffmpeg lookup used by the packaged .dmg / .exe."""

    def setUp(self):
        self._saved = os.environ.get(core.FFMPEG_DIR_ENV)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(core.FFMPEG_DIR_ENV, None)
        else:
            os.environ[core.FFMPEG_DIR_ENV] = self._saved

    def test_env_override_wins_over_path(self):
        with tempfile.TemporaryDirectory() as d:
            name = "ffmpeg.exe" if core.sys.platform == "win32" else "ffmpeg"
            fake = os.path.join(d, name)
            with open(fake, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(fake, 0o755)
            os.environ[core.FFMPEG_DIR_ENV] = d
            self.assertEqual(core.which_ffmpeg("ffmpeg"), fake)

    def test_missing_tool_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ[core.FFMPEG_DIR_ENV] = d
            self.assertIsNone(core.which_ffmpeg("definitely-not-a-real-tool-xyz"))

    def test_exec_bit_is_restored(self):
        with tempfile.TemporaryDirectory() as d:
            name = "ffprobe.exe" if core.sys.platform == "win32" else "ffprobe"
            fake = os.path.join(d, name)
            with open(fake, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(fake, 0o644)          # lost the exec bit
            os.environ[core.FFMPEG_DIR_ENV] = d
            self.assertEqual(core.which_ffmpeg("ffprobe"), fake)
            self.assertTrue(os.access(fake, os.X_OK))

    def test_absolute_path_passthrough(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            path = fh.name
        try:
            self.assertEqual(core.which_ffmpeg(path), path)
        finally:
            os.remove(path)

    def test_bundle_dirs_include_app_siblings(self):
        # Simulate a frozen macOS .app: Contents/Frameworks is _MEIPASS.
        original = getattr(core.sys, "_MEIPASS", None)
        core.sys._MEIPASS = "/Apps/X.app/Contents/Frameworks"
        try:
            dirs = [os.path.normpath(p) for p in core._bundle_dirs()]
            self.assertIn("/Apps/X.app/Contents/Frameworks", dirs)
            self.assertIn("/Apps/X.app/Contents/Resources", dirs)
            self.assertIn("/Apps/X.app/Contents/MacOS", dirs)
        finally:
            if original is None:
                del core.sys._MEIPASS
            else:
                core.sys._MEIPASS = original


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
