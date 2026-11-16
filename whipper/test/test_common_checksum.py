# -*- Mode: Python; test-case-name: whipper.test.test_common_checksum -*-
# vi:si:et:sw=4:sts=4:ts=4

import binascii
import os
import struct
import tempfile
import wave

from whipper.common import checksum
from whipper.extern.task import task
from whipper.test import common


def _write_wav(path, data, sampwidth=2, nchannels=2, framerate=44100):
    w = wave.open(path, 'wb')
    w.setparams((nchannels, sampwidth, framerate, 0, 'NONE', 'NONE'))
    w.writeframes(data)
    w.close()


class CRC32TaskTestCase(common.TestCase):

    def setUp(self):
        self.runner = task.SyncRunner(verbose=False)
        self.dir = tempfile.mkdtemp(suffix='.whipper.test.checksum')
        # 1000 "frames" (wave frames) of deterministic data
        self.data = b''.join(
            struct.pack('<hh', i % 32768, -i % 32768) for i in range(1000))
        self.path = os.path.join(self.dir, 'test.wav')
        _write_wav(self.path, self.data)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run_task(self, t):
        self.runner.run(t)
        return t.checksum

    def testWholeFile(self):
        t = checksum.CRC32Task(self.path)
        self.assertEqual(self._run_task(t), binascii.crc32(self.data) & 0xffffffff)

    def testSampleRange(self):
        # checksum of a sub-range must equal the crc32 of just that slice
        t = checksum.CRC32Task(self.path, sampleStart=100, sampleLength=500)
        expected = binascii.crc32(self.data[400:2400]) & 0xffffffff
        self.assertEqual(self._run_task(t), expected)

    def testSampleRangeToEnd(self):
        t = checksum.CRC32Task(self.path, sampleStart=900, sampleLength=-1)
        expected = binascii.crc32(self.data[3600:]) & 0xffffffff
        self.assertEqual(self._run_task(t), expected)

    def testChainedEqualsWhole(self):
        # two chained tasks over consecutive ranges must produce the same
        # checksum as one task over the whole file
        running = checksum.RunningChecksum()
        t1 = checksum.CRC32Task(self.path, sampleStart=0, sampleLength=400,
                                chain=running)
        t2 = checksum.CRC32Task(self.path, sampleStart=400, sampleLength=-1,
                                chain=running)
        self._run_task(t1)
        self._run_task(t2)
        self.assertEqual(t2.checksum, binascii.crc32(self.data) & 0xffffffff)
        self.assertEqual(running.value & 0xffffffff, t2.checksum)

    def testChainedAcrossFiles(self):
        # chaining works across separate chunk files too: the cumulative
        # checksum equals the crc32 of the concatenated data
        data1, data2 = self.data[:2000], self.data[2000:]
        p1 = os.path.join(self.dir, 'c1.wav')
        p2 = os.path.join(self.dir, 'c2.wav')
        _write_wav(p1, data1)
        _write_wav(p2, data2)
        running = checksum.RunningChecksum()
        self._run_task(checksum.CRC32Task(p1, chain=running))
        t2 = checksum.CRC32Task(p2, chain=running)
        self._run_task(t2)
        self.assertEqual(t2.checksum,
                         binascii.crc32(self.data) & 0xffffffff)
