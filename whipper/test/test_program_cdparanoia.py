# -*- Mode: Python; test-case-name: whipper.test.test_program_cdparanoia -*-
# vi:si:et:sw=4:sts=4:ts=4

import binascii
import os
import shutil
import struct
import tempfile
import wave
from types import SimpleNamespace
from unittest import mock

from whipper.common import checksum
from whipper.extern.task import task

from whipper.program import cdparanoia

from whipper.test import common


class ParseTestCase(common.TestCase):

    def setUp(self):
        # report from Afghan Whigs - Sweet Son Of A Bitch
        path = os.path.join(os.path.dirname(__file__),
                            'cdparanoia.progress')
        self._parser = cdparanoia.ProgressParser(start=45990, stop=47719)

        self._handle = open(path)

    def testParse(self):
        for line in self._handle.readlines():
            self._parser.parse(line)

        q = '%.01f %%' % (self._parser.getTrackQuality() * 100.0, )
        self.assertEqual(q, '99.6 %')


class Parse1FrameTestCase(common.TestCase):

    def setUp(self):
        path = os.path.join(os.path.dirname(__file__),
                            'cdparanoia.progress.strokes')
        self._parser = cdparanoia.ProgressParser(start=0, stop=0)

        self._handle = open(path)

    def testParse(self):
        for line in self._handle.readlines():
            self._parser.parse(line)

        q = '%.01f %%' % (self._parser.getTrackQuality() * 100.0, )
        self.assertEqual(q, '100.0 %')


class ErrorTestCase(common.TestCase):

    def setUp(self):
        # report from a rip with offset -1164 causing scsi errors
        path = os.path.join(os.path.dirname(__file__),
                            'cdparanoia.progress.error')
        self._parser = cdparanoia.ProgressParser(start=0, stop=10800)

        self._handle = open(path)

    def testParse(self):
        for line in self._handle.readlines():
            self._parser.parse(line)

        q = '%.01f %%' % (self._parser.getTrackQuality() * 100.0, )
        self.assertEqual(q, '79.6 %')


class VersionTestCase(common.TestCase):

    def testGetVersion(self):
        v = cdparanoia.getCdParanoiaVersion()
        self.assertTrue(v)


class AnalyzeFileTask(cdparanoia.AnalyzeTask):

    def __init__(self, path):
        self.command = ['cat', path]

    def readbytesout(self, bytes_stdout):
        self.readbyteserr(bytes_stdout)


class CacheTestCase(common.TestCase):

    def testDefeatsCache(self):
        self.runner = task.SyncRunner(verbose=False)

        path = os.path.join(os.path.dirname(__file__),
                            'cdparanoia', 'PX-L890SA.cdparanoia-A.stderr')
        t = AnalyzeFileTask(path)
        self.runner.run(t)
        self.assertTrue(t.defeatsCache)


class ChunkRangesTestCase(common.TestCase):

    def testDisabledGivesSingleChunk(self):
        self.assertEqual(cdparanoia.chunkRanges(0, 99, 0), [(0, 99)])
        self.assertEqual(cdparanoia.chunkRanges(0, 99, -5), [(0, 99)])

    def testChunkBiggerThanTrackGivesSingleChunk(self):
        self.assertEqual(cdparanoia.chunkRanges(0, 99, 100), [(0, 99)])

    def testChunks(self):
        self.assertEqual(cdparanoia.chunkRanges(0, 99, 30),
                         [(0, 29), (30, 59), (60, 89), (90, 99)])

    def testChunksWithStartOffset(self):
        self.assertEqual(cdparanoia.chunkRanges(100, 250, 75),
                         [(100, 174), (175, 249), (250, 250)])


def _write_wav(path, data):
    w = wave.open(path, 'wb')
    w.setparams((2, 2, 44100, 0, 'NONE', 'NONE'))
    w.writeframes(data)
    w.close()


class _FakeParent:
    """Minimal stand-in for ReadVerifyTrackTask as a VerifyChunkTask parent."""

    def __init__(self):
        self._appended = 0
        self._chunkAttempts = {}
        self._runningTest = checksum.RunningChecksum()
        self._runningVerify = checksum.RunningChecksum()
        self.retries = []

    def _insertRetry(self, cs, ce):
        self.retries.append((cs, ce))


class VerifyChunkTaskTestCase(common.TestCase):

    def setUp(self):
        self.runner = task.SyncRunner(verbose=False)
        self.dir = tempfile.mkdtemp(suffix='.whipper.test.chunk')
        self.data = b''.join(
            struct.pack('<hh', i % 32768, -i % 32768) for i in range(100))
        self.testPath = os.path.join(self.dir, 'test.wav')
        self.verifyPath = os.path.join(self.dir, 'verify.wav')
        self.outPath = os.path.join(self.dir, 'out.wav')
        _write_wav(self.testPath, self.data)
        _write_wav(self.verifyPath, self.data)
        open(self.outPath, 'wb').close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _crc(self, value):
        return SimpleNamespace(checksum=value)

    def testMatchingChecksumsAppendChunk(self):
        parent = _FakeParent()
        t = cdparanoia.VerifyChunkTask(
            parent, self._crc(0xdeadbeef), self._crc(0xdeadbeef),
            self.testPath, self.verifyPath, self.outPath,
            start=10, stop=19, totalDataBytes=len(self.data))
        self.runner.run(t)

        with open(self.outPath, 'rb') as f:
            out = f.read()
        # output is the wav header (44 bytes) plus the audio data
        self.assertEqual(len(out), 44 + len(self.data))
        self.assertEqual(out[44:], self.data)
        # the header size fields cover the whole track, not just the chunk
        self.assertEqual(struct.unpack('<I', out[40:44])[0], len(self.data))
        self.assertEqual(struct.unpack('<I', out[4:8])[0], 36 + len(self.data))
        # chunk temp files are cleaned up
        self.assertFalse(os.path.exists(self.testPath))
        self.assertFalse(os.path.exists(self.verifyPath))
        # the chunk was counted as appended and folded into the running CRC
        self.assertEqual(parent._appended, 1)
        self.assertEqual(parent._runningTest.value & 0xffffffff,
                         binascii.crc32(self.data) & 0xffffffff)

    def testMismatchRaisesWithFrameRangeWhenAttemptsExhausted(self):
        parent = _FakeParent()
        t = cdparanoia.VerifyChunkTask(
            parent, self._crc(0x11111111), self._crc(0x22222222),
            self.testPath, self.verifyPath, self.outPath,
            start=10, stop=19, totalDataBytes=len(self.data), maxAttempts=1)
        try:
            self.runner.run(t)
            self.fail('expected TaskException')
        except task.TaskException as e:
            self.assertIsInstance(e.exception, cdparanoia.ChecksumException)
            self.assertIn('frames 10-19', str(e.exception))
        # no retry was queued with maxAttempts=1
        self.assertEqual(parent.retries, [])
        # nothing was appended to the output
        self.assertEqual(os.path.getsize(self.outPath), 0)

    def testMismatchQueuesRetryForSameChunkWithoutFailing(self):
        parent = _FakeParent()
        t = cdparanoia.VerifyChunkTask(
            parent, self._crc(0x11111111), self._crc(0x22222222),
            self.testPath, self.verifyPath, self.outPath,
            start=10, stop=19, totalDataBytes=len(self.data), maxAttempts=3)
        # with attempts remaining, the task queues a retry of its own chunk
        # and completes without an exception
        self.runner.run(t)
        self.assertEqual(parent.retries, [(10, 19)])
        self.assertEqual(parent._chunkAttempts, {(10, 19): 1})
        self.assertEqual(parent._appended, 0)
        # nothing was appended and the failed attempt's temps were cleaned
        self.assertEqual(os.path.getsize(self.outPath), 0)
        self.assertFalse(os.path.exists(self.testPath))
        self.assertFalse(os.path.exists(self.verifyPath))


class ReadVerifyTrackTaskStructureTestCase(common.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(suffix='.whipper.test.rvt')
        self.out = os.path.join(self.dir, 'track.flac')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _make(self, start, stop, verifyChunkSize):
        return cdparanoia.ReadVerifyTrackTask(
            self.out, None, start, stop, False, verifyChunkSize=verifyChunkSize)

    def testWholeTrackByDefault(self):
        t = self._make(100, 199, 0)
        # one chunk: read, crc, verify read, crc, append, encode, crc, peak,
        # tag, embed
        self.assertEqual(len(t.tasks), 10)
        self.assertIsInstance(t.tasks[0], cdparanoia.ReadTrackTask)
        self.assertEqual((t.tasks[0]._start, t.tasks[0]._stop), (100, 199))
        self.assertIsInstance(t.tasks[4], cdparanoia.VerifyChunkTask)
        self.assertIs(t.tasks[4]._parent, t)

    def testChunkedTasks(self):
        t = self._make(0, 24, 10)
        # 3 chunks of 5 tasks each, plus encode, crc, peak, tag, embed
        self.assertEqual(len(t.tasks), 3 * 5 + 5)
        for i in range(3):
            base = i * 5
            self.assertIsInstance(t.tasks[base], cdparanoia.ReadTrackTask)
            self.assertIsInstance(t.tasks[base + 1], checksum.CRC32Task)
            self.assertIsInstance(t.tasks[base + 2], cdparanoia.ReadTrackTask)
            self.assertIsInstance(t.tasks[base + 3], checksum.CRC32Task)
            self.assertIsInstance(t.tasks[base + 4], cdparanoia.VerifyChunkTask)
            self.assertEqual(t.tasks[base]._start, i * 10)
            self.assertEqual(t.tasks[base]._stop, min(i * 10 + 9, 24))
            self.assertIs(t.tasks[base + 4]._parent, t)
        # chunk checksums are compared per chunk, not chained up front
        self.assertIsNone(t.tasks[1].chain)
        self.assertIsNone(t.tasks[3].chain)


class _NoopTask(task.Task):
    peak = 0

    def __init__(self, *args, **kwargs):
        pass

    def start(self, runner):
        task.Task.start(self, runner)
        self.schedule(0.0, self.stop)


def _chunk_data(start, stop, corrupt=False):
    data = b''.join(
        struct.pack('<hh', f % 32768, (-f) % 32768)
        for f in range(start, stop + 1))
    if corrupt:
        data = data[:-1] + bytes([data[-1] ^ 0xff])
    return data


class _FakeReadTrackTask(cdparanoia.ReadTrackTask):
    """ReadTrackTask that writes deterministic WAV data instead of
    invoking cd-paranoia; the first N verify reads can be corrupted."""

    corrupt_verify = False
    corrupt_verify_reads = 0  # corrupt this many verify reads, then behave
    verify_reads = 0

    def __init__(self, *args, **kwargs):
        self._action = kwargs.get('action', 'Reading')
        super().__init__(*args, **kwargs)

    def start(self, runner):
        task.Task.start(self, runner)
        self.schedule(0.0, self._fake_read)

    def _fake_read(self):
        corrupt = False
        if self._action == 'Verifying':
            type(self).verify_reads += 1
            if (type(self).corrupt_verify and
                    type(self).verify_reads <= type(self).corrupt_verify_reads):
                corrupt = True
        _write_wav(self.path, _chunk_data(self._start, self._stop,
                                          corrupt=corrupt))
        frames = self._stop - self._start + 1
        self._parser.reads = 2 * frames
        self.duration = frames / 75.0
        self.stop()


class ReadVerifyTrackTaskRunTestCase(common.TestCase):
    """Run the whole chunked read/verify flow with a fake drive."""

    def setUp(self):
        self.runner = task.SyncRunner(verbose=False)
        self.dir = tempfile.mkdtemp(suffix='.whipper.test.rvt.run')
        self.out = os.path.join(self.dir, 'track.flac')
        from whipper.common import encode
        self._patches = [
            mock.patch.object(cdparanoia, 'ReadTrackTask', _FakeReadTrackTask),
            mock.patch.object(encode, 'FlacEncodeTask', _NoopTask),
            mock.patch.object(encode, 'SoxPeakTask', _NoopTask),
            mock.patch.object(encode, 'TaggingTask', _NoopTask),
            mock.patch.object(encode, 'EmbedPictureTask', _NoopTask),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        _FakeReadTrackTask.corrupt_verify = False
        _FakeReadTrackTask.corrupt_verify_reads = 0
        _FakeReadTrackTask.verify_reads = 0
        shutil.rmtree(self.dir, ignore_errors=True)

    def _make(self, start, stop, verifyChunkSize):
        return cdparanoia.ReadVerifyTrackTask(
            self.out, None, start, stop, False, verifyChunkSize=verifyChunkSize)

    def testChunkedRunAssemblesWholeTrack(self):
        t = self._make(0, 24, 10)
        self.runner.run(t)

        expected = binascii.crc32(_chunk_data(0, 24)) & 0xffffffff
        self.assertEqual(t.testchecksum, expected)
        self.assertEqual(t.copychecksum, expected)
        self.assertEqual(t.checksum, expected)
        self.assertEqual(t.quality, 1.0)
        # the output file was moved into place, temps cleaned up
        self.assertTrue(os.path.exists(self.out))
        self.assertFalse(os.path.exists(self.out + '.part'))
        self.assertFalse(os.path.exists(t._tmpwavpath))
        for testpath, verifypath in t._chunkPaths:
            self.assertFalse(os.path.exists(testpath))
            self.assertFalse(os.path.exists(verifypath))

    def testChunkedRunDetectsCorruptVerifyPassWithRange(self):
        _FakeReadTrackTask.corrupt_verify = True
        _FakeReadTrackTask.corrupt_verify_reads = 100  # always corrupt
        t = self._make(0, 24, 10)
        try:
            self.runner.run(t)
            self.fail('expected TaskException')
        except task.TaskException as e:
            self.assertIsInstance(e.exception, cdparanoia.ChecksumException)
            # first chunk is frames 0-9
            self.assertIn('frames 0-9', str(e.exception))
        # the failing chunk was retried up to maxChunkAttempts (default 5)
        # before the track failed; later chunks were never read
        self.assertEqual(_FakeReadTrackTask.verify_reads, 5)
        self.assertFalse(os.path.exists(self.out))
        for testpath, verifypath in t._chunkPaths:
            self.assertFalse(os.path.exists(testpath))
            self.assertFalse(os.path.exists(verifypath))

    def testTransientMismatchRetriesOnlyThatChunk(self):
        # corrupt just the first verify read (of chunk frames 0-9)
        _FakeReadTrackTask.corrupt_verify = True
        _FakeReadTrackTask.corrupt_verify_reads = 1
        t = self._make(0, 24, 10)
        self.runner.run(t)  # must not raise

        expected = binascii.crc32(_chunk_data(0, 24)) & 0xffffffff
        self.assertEqual(t.testchecksum, expected)
        self.assertEqual(t.copychecksum, expected)
        self.assertEqual(t.checksum, expected)
        # 3 chunks x 1 verify read + 1 extra verify read for the retried
        # chunk; the other chunks were read exactly once per pass
        self.assertEqual(_FakeReadTrackTask.verify_reads, 3 + 1)
        self.assertTrue(os.path.exists(self.out))
