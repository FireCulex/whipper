# -*- Mode: Python; test-case-name: whipper.test.test_common_checksum -*-
# vi:si:et:sw=4:sts=4:ts=4

# Copyright (C) 2009 Thomas Vander Stichele

# This file is part of whipper.
#
# whipper is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# whipper is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with whipper.  If not, see <http://www.gnu.org/licenses/>.

import binascii
import wave
import tempfile
import subprocess
import os


from whipper.extern.task import task as etask

import logging
logger = logging.getLogger(__name__)

# checksums are not CRC's. a CRC is a specific type of checksum.


class RunningChecksum:
    """
    Mutable holder for a cumulative CRC32 value.

    Pass the same instance as ``running`` to several CRC32Tasks that run
    in order; each task continues the CRC from the holder's current value,
    so the final value equals the CRC32 of all the data combined.
    """

    def __init__(self, value=0):
        self.value = value


class CRC32Task(etask.Task):
    # how many bytes to read at once when computing the checksum
    _BLOCK_SIZE = 1024 * 1024

    def __init__(self, path, sampleStart=0, sampleLength=-1, is_wave=True,
                 chain=None):
        """
        Init CRC32Task.

        :param path: path of the file to checksum
        :param sampleStart: first sample frame (wave frame) to include
        :param sampleLength: number of sample frames to include,
                             -1 for all remaining
        :param is_wave: whether the file is a WAV; if False it is decoded
                        with flac first
        :param chain: optional RunningChecksum to chain this checksum into
        """
        self.path = path
        self.is_wave = is_wave
        self.sampleStart = sampleStart
        self.sampleLength = sampleLength
        self.chain = chain
        self.checksum = None

    def start(self, runner):
        etask.Task.start(self, runner)
        self.schedule(0.0, self._crc32)

    def _crc32(self):
        if not self.is_wave:
            _, tmpf = tempfile.mkstemp()

            try:
                subprocess.check_call(['flac', '-d', self.path, '-fo', tmpf])

                w = wave.open(tmpf)
            finally:
                os.remove(tmpf)
        else:
            w = wave.open(self.path)

        crc = self.chain.value if self.chain is not None else 0
        bytes_per_frame = w.getsampwidth() * w.getnchannels()
        block_frames = max(1, self._BLOCK_SIZE // bytes_per_frame)
        w.setpos(self.sampleStart)
        remaining = self.sampleLength
        while remaining != 0:
            if remaining < 0:
                nframes = block_frames
            else:
                nframes = min(remaining, block_frames)
            d = w.readframes(nframes)
            if not d:
                break
            crc = binascii.crc32(d, crc)
            if remaining > 0:
                remaining -= nframes

        self.checksum = crc & 0xffffffff
        if self.chain is not None:
            self.chain.value = crc
        self.stop()
