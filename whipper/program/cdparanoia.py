# -*- Mode: Python; test-case-name: whipper.test.test_program_cdparanoia -*-
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

import errno
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time

from whipper.common import common
from whipper.common import task as ctask
from whipper.extern import asyncsub
from whipper.extern.task import task

import logging
logger = logging.getLogger(__name__)


class FileSizeError(Exception):
    """The given path does not have the expected size."""

    message = None

    def __init__(self, path, message):
        self.args = (path, message)
        self.path = path
        self.message = message


class ReturnCodeError(Exception):
    """The program had a non-zero return code."""

    def __init__(self, returncode):
        self.args = (returncode, )
        self.returncode = returncode


class ChecksumException(Exception):
    pass


# example:
# ##: 0 [read] @ 24696
_PROGRESS_RE = re.compile(r"""
    ^\#\#: (?P<code>.+)\s         # function code
    \[(?P<function>.*)\]\s@\s     # [function name] @
    (?P<offset>\d+)               # offset in words (2-byte one channel value)
""", re.VERBOSE)

_ERROR_RE = re.compile("^scsi_read error:")

# from reading cdparanoia source code, it looks like offset is reported in
# number of single-channel samples, ie. 2 bytes (word) per unit, and absolute


class ProgressParser:
    read = 0  # last [read] frame
    wrote = 0  # last [wrote] frame
    errors = 0  # count of number of scsi errors
    _nframes = None  # number of frames read on each [read]
    _firstFrames = None  # number of frames read on first [read]
    reads = 0  # total number of reads

    def __init__(self, start, stop):
        """
        Init ProgressParser.

        :param start: first frame to rip
        :type start: int
        :param stop: last frame to rip (inclusive)
        :type stop: int
        """
        self.start = start
        self.stop = stop

        # FIXME: privatize
        self.read = start

        self._reads = {}  # read count for each sector

    def parse(self, line):
        """Parse a line."""
        m = _PROGRESS_RE.search(line)
        if m:
            # code = int(m.group('code'))
            function = m.group('function')
            wordOffset = int(m.group('offset'))
            if function == 'read':
                self._parse_read(wordOffset)
            elif function == 'wrote':
                self._parse_wrote(wordOffset)

        m = _ERROR_RE.search(line)
        if m:
            self.errors += 1

    def _parse_read(self, wordOffset):
        if wordOffset % common.WORDS_PER_FRAME != 0:
            logger.debug('THOMAS: not a multiple of %d: %d',
                         common.WORDS_PER_FRAME, wordOffset)
            return

        frameOffset = wordOffset / common.WORDS_PER_FRAME

        # set nframes if not yet set
        if self._nframes is None and self.read != 0:
            self._nframes = frameOffset - self.read
            logger.debug('set nframes to %r', self._nframes)

        # set firstFrames if not yet set
        if self._firstFrames is None:
            self._firstFrames = frameOffset - self.start
            logger.debug('set firstFrames to %r', self._firstFrames)

        markStart = None
        markEnd = None  # the next unread frame (half-inclusive)

        # verify it either read nframes more or went back for verify
        if frameOffset > self.read:
            delta = frameOffset - self.read
            if self._nframes and delta != self._nframes:
                # my drive either reads 7 or 13 frames
                pass

            # update our read sectors hash
            markStart = self.read
            markEnd = frameOffset
        else:
            # went back to verify
            # we could use firstFrames as an estimate on how many frames this
            # read, but this lowers our track quality needlessly where
            # EAC still reports 100% track quality
            markStart = frameOffset  # - self._firstFrames
            markEnd = frameOffset

        # FIXME: doing this is way too slow even for a testcase, so disable
        # for frame in range(markStart, markEnd):
        #     if frame not in list(self._reads.keys()):
        #         self._reads[frame] = 0
        #     self._reads[frame] += 1

        # cdparanoia reads quite a bit beyond the current track before it
        # goes back to verify; don't count those
        # markStart, markEnd of 0, 21 with stop 0 should give 1 read
        if markEnd > self.stop + 1:
            markEnd = self.stop + 1
        if markStart > self.stop + 1:
            markStart = self.stop + 1

        self.reads += markEnd - markStart

        # update our read pointer
        self.read = frameOffset

    def _parse_wrote(self, wordOffset):
        # cdparanoia outputs most [wrote] calls with one word less than a frame
        frameOffset = (wordOffset + 1) / common.WORDS_PER_FRAME
        self.wrote = frameOffset

    def getTrackQuality(self):
        """
        Each frame gets read twice.

        More than two reads for a frame reduce track quality.
        """
        frames = self.stop - self.start + 1  # + 1 since stop is inclusive
        reads = self.reads
        logger.debug('getTrackQuality: frames %d, reads %d', frames, reads)

        try:
            # don't go over a 100%
            # we know that cdparanoia reads each frame at least twice
            return min(frames * 2.0 / reads, 1.0)
        except ZeroDivisionError:
            raise RuntimeError("cdparanoia couldn't read any frames "
                               "for the current track")

# FIXME: handle errors


class ReadTrackTask(task.Task):
    """Task that reads a track using cdparanoia."""

    description = "Reading track"
    quality = None  # set at end of reading
    speed = None
    duration = None  # in seconds

    _MAXERROR = 100  # number of errors detected by parser

    def __init__(self, path, table, start, stop, overread, offset=0,
                 device=None, action="Reading", what="track"):
        """
        Read the given track.

        :param path: where to store the ripped track
        :type path: str
        :param table: table of contents of CD
        :type table: table.Table
        :param start: first frame to rip
        :type start: int
        :param stop: last frame to rip (inclusive); >= start
        :type stop: int
        :param offset: read offset, in samples
        :type offset: int
        :param device: the device to rip from
        :type device: str
        :param action: a string representing the action; e.g. Read/Verify
        :type action: str
        :param what: a string representing what's being read; e.g. Track
        :type what: str
        """
        assert isinstance(path, str), "%r is not str" % path

        self.path = path
        self._table = table
        self._start = start
        self._stop = stop
        self._offset = offset
        self._parser = ProgressParser(start, stop)
        self._device = device
        self._start_time = None
        self._overread = overread

        self._buffer = ""  # accumulate characters
        self._errors = []
        self.description = "%s %s" % (action, what)

    def start(self, runner):
        task.Task.start(self, runner)

        # find on which track the range starts and stops
        startTrack = 0
        startOffset = 0
        stopTrack = 0
        stopOffset = self._stop

        for i, _ in enumerate(self._table.tracks):
            if self._table.getTrackStart(i + 1) <= self._start:
                startTrack = i + 1
                startOffset = self._start - self._table.getTrackStart(i + 1)
            if self._table.getTrackEnd(i + 1) <= self._stop:
                stopTrack = i + 1
                stopOffset = self._stop - self._table.getTrackStart(i + 1)

        logger.debug('ripping from %d to %d (inclusive)', self._start,
                     self._stop)
        logger.debug('starting at track %d, offset %d', startTrack,
                     startOffset)
        logger.debug('stopping at track %d, offset %d', stopTrack, stopOffset)

        bufsize = 1024
        if self._overread:
            argv = ["cd-paranoia", "--stderr-progress",
                    "--sample-offset=%d" % self._offset, "--force-overread", ]
        else:
            argv = ["cd-paranoia", "--stderr-progress",
                    "--sample-offset=%d" % self._offset, ]
        if self._device:
            argv.extend(["--force-cdrom-device", self._device, ])
        argv.extend(["%d[%s]-%d[%s]" % (
            startTrack, common.framesToHMSF(startOffset),
            stopTrack, common.framesToHMSF(stopOffset)),
            self.path])
        logger.debug('running %s', (" ".join(argv), ))
        if self._offset > 587:
            logger.warning(
                "because of a cd-paranoia upstream bug whipper may fail to "
                "work correctly when using offset values > 587 (current "
                "value: %d) and print warnings like this: 'file size 0 did "
                "not match expected size'. For more details please check the "
                "following issues: "
                "https://github.com/whipper-team/whipper/issues/234 and "
                "https://github.com/rocky/libcdio-paranoia/issues/14",
                self._offset
            )
        if stopTrack == 99:
            logger.warning(
                "because of a cd-paranoia upstream bug whipper may fail to "
                "rip the last track of a CD when it has got 99 of them. "
                "For more details please check the following issue: "
                "https://github.com/whipper-team/whipper/issues/302"
            )
        try:
            self._popen = asyncsub.Popen(argv,
                                         bufsize=bufsize,
                                         stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE,
                                         close_fds=True)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise common.MissingDependencyException('cd-paranoia')

            raise

        self._start_time = time.time()
        self.schedule(1.0, self._read, runner)

    def _read(self, runner):
        ret = self._popen.recv_err()
        if not ret:
            if self._popen.poll() is not None:
                self._done()
                return
            self.schedule(0.01, self._read, runner)
            return

        self._buffer += ret.decode()

        # parse buffer into lines if possible, and parse them
        if "\n" in self._buffer:
            lines = self._buffer.split('\n')
            if lines[-1] != "\n":
                # last line didn't end yet
                self._buffer = lines[-1]
                del lines[-1]
            else:
                self._buffer = ""

            for line in lines:
                self._parser.parse(line)

            # fail if too many errors
            if self._parser.errors > self._MAXERROR:
                logger.debug('%d errors, terminating', self._parser.errors)
                self._popen.terminate()

            num = self._parser.wrote - self._start + 1
            den = self._stop - self._start + 1
            assert den != 0, "stop %d should be >= start %d" % (
                self._stop, self._start)
            progress = float(num) / float(den)
            if progress < 1.0:
                self.setProgress(progress)

        # 0 does not give us output before we complete, 1.0 gives us output
        # too late
        self.schedule(0.01, self._read, runner)

    def _poll(self, runner):
        if self._popen.poll() is None:
            self.schedule(1.0, self._poll, runner)
            return

        self._done()

    def _done(self):
        end_time = time.time()
        self.setProgress(1.0)

        # check if the length matches
        size = os.stat(self.path)[stat.ST_SIZE]
        # wav header is 44 bytes
        offsetLength = self._stop - self._start + 1
        expected = offsetLength * common.BYTES_PER_FRAME + 44
        if size != expected:
            # FIXME: handle errors better
            logger.warning('file size %d did not match expected size %d',
                           size, expected)
            if (size - expected) % common.BYTES_PER_FRAME == 0:
                logger.warning('%d frames difference', (
                    (size - expected) / common.BYTES_PER_FRAME))
            else:
                logger.warning('non-integral amount of frames difference')

            self.setAndRaiseException(FileSizeError(self.path,
                                                    "File size %d did not "
                                                    "match expected "
                                                    "size %d" % (
                                                        size, expected)))

        if not self.exception and self._popen.returncode != 0:
            if self._errors:
                print("\n".join(self._errors))
            else:
                logger.warning('exit code %r', self._popen.returncode)
                self.exception = ReturnCodeError(self._popen.returncode)

        self.quality = self._parser.getTrackQuality()
        self.duration = end_time - self._start_time
        self.speed = (offsetLength / 75.0) / self.duration

        self.stop()
        return


def chunkRanges(start, stop, chunkSize):
    """
    Split the inclusive frame range [start, stop] into chunks.

    :param start: first frame (inclusive)
    :param stop: last frame (inclusive)
    :param chunkSize: maximum number of frames per chunk;
                      <= 0 means a single chunk covering the whole range
    :returns: list of (chunkStart, chunkStop) inclusive tuples
    :rtype: list(tuple(int, int))
    """
    if chunkSize <= 0 or stop - start + 1 <= chunkSize:
        return [(start, stop)]
    return [(cs, min(cs + chunkSize - 1, stop))
            for cs in range(start, stop + 1, chunkSize)]


class VerifyChunkTask(task.Task):
    """
    Compare the test and verify checksums of one chunk of frames.

    If they match, the chunk's audio data is appended to the output WAV
    (including the WAV header for the first chunk, with its size fields
    fixed up to cover the whole track) and the chunk's temporary files
    are removed. If they don't match, a ChecksumException naming the
    frame range is raised.
    """

    logCategory = 'VerifyChunkTask'
    description = 'Verifying chunk'

    # cdparanoia writes a standard 44 byte WAV header; ReadTrackTask
    # verifies the file size is exactly header + frames * BYTES_PER_FRAME
    _WAV_HEADER_SIZE = 44
    _BLOCK_SIZE = 1024 * 1024

    def __init__(self, testCrc, verifyCrc, testPath, verifyPath, outPath,
                 start, stop, first, totalDataBytes=None):
        """
        Init VerifyChunkTask.

        :param testCrc: CRC32Task that checksummed the test read of the chunk
        :param verifyCrc: CRC32Task that checksummed the verify read
        :param testPath: path of the WAV holding the test read
        :param verifyPath: path of the WAV holding the verify read
        :param outPath: path of the output WAV to append the chunk to
        :param start: first frame of the chunk (inclusive)
        :param stop: last frame of the chunk (inclusive)
        :param first: whether this is the first chunk of the track
        :param totalDataBytes: audio bytes of the whole track, used to fix
                               up the WAV header of the first chunk
        """
        self._testCrc = testCrc
        self._verifyCrc = verifyCrc
        self._testPath = testPath
        self._verifyPath = verifyPath
        self._outPath = outPath
        self._start = start
        self._stop = stop
        self._first = first
        self._totalDataBytes = totalDataBytes
        self.description = 'Verifying frames %d-%d' % (start, stop)

    def start(self, runner):
        task.Task.start(self, runner)
        self.schedule(0.0, self._verify)

    def _verify(self):
        if self._testCrc.checksum != self._verifyCrc.checksum:
            self.setAndRaiseException(ChecksumException(
                'read and verify failed for frames %d-%d: test checksum '
                '%08x, verify checksum %08x' % (
                    self._start, self._stop,
                    self._testCrc.checksum, self._verifyCrc.checksum)))
            self.stop()
            return

        with open(self._testPath, 'rb') as src, \
                open(self._outPath, 'ab') as dst:
            header = bytearray(src.read(self._WAV_HEADER_SIZE))
            if self._first:
                if self._totalDataBytes is not None:
                    # the chunk header only covers the chunk; fix up the
                    # RIFF and data size fields to cover the whole track
                    struct.pack_into('<I', header, 4,
                                     36 + self._totalDataBytes)
                    struct.pack_into('<I', header, 40, self._totalDataBytes)
                dst.write(bytes(header))
            while True:
                block = src.read(self._BLOCK_SIZE)
                if not block:
                    break
                dst.write(block)

        os.unlink(self._testPath)
        os.unlink(self._verifyPath)
        self.stop()


class ReadVerifyTrackTask(task.MultiSeparateTask):
    """
    Task that reads and verifies a track using cdparanoia.

    It also encodes the track.

    The path where the file is stored can be changed if necessary, for
    example if the file name is too long.

    :cvar checksum: the checksum of the track; set if they match
    :cvar testchecksum: the test checksum of the track
    :cvar copychecksum: the copy checksum of the track
    :cvar testspeed: the test speed of the track, as a multiple of
                     track duration
    :cvar copyspeed: the copy speed of the track, as a multiple of
                     track duration
    :cvar testduration: the test duration of the track, in seconds
    :cvar copyduration: the copy duration of the track, in seconds
    :cvar peak: the peak level of the track
    """

    checksum = None
    testchecksum = None
    copychecksum = None
    peak = None
    quality = None
    testspeed = None
    copyspeed = None
    testduration = None
    copyduration = None

    _tmpwavpath = None
    _tmppath = None

    def __init__(self, path, table, start, stop, overread, offset=0,
                 device=None, taglist=None, what="track", coverArtPath=None,
                 verifyChunkSize=0):
        """
        Init ReadVerifyTrackTask.

        :param path: where to store the ripped track
        :type path: str
        :param table: table of contents of CD
        :type table: table.Table
        :param start: first frame to rip
        :type start: int
        :param stop: last frame to rip (inclusive)
        :type stop: int
        :param offset: read offset, in samples
        :type offset: int
        :param device: the device to rip from
        :type device: str
        :param taglist: a dict of tags
        :type taglist: dict
        :param verifyChunkSize: maximum frames per verified chunk; each
                                chunk is read twice and its checksums
                                compared before the next chunk is read,
                                so a mismatch is reported with its frame
                                range; 0 verifies the whole track at once
        :type verifyChunkSize: int
        """
        task.MultiSeparateTask.__init__(self)

        logger.debug('creating read and verify task on %r', path)

        if taglist:
            logger.debug('read and verify with taglist %r', taglist)
        # FIXME: choose a dir on the same disk/dir as the final path
        fd, tmppath = tempfile.mkstemp(suffix='.whipper.wav')
        os.fchmod(fd, 0o644)
        os.close(fd)
        self._tmpwavpath = tmppath
        self._start = start
        self._stop = stop

        from whipper.common import checksum

        self.tasks = []
        self._chunkPaths = []
        self._readTasks = []
        self._verifyTasks = []
        # the per-chunk CRCs of each pass are chained, so they combine into
        # the same value a whole-track CRC would give
        self._runningTest = checksum.RunningChecksum()
        self._runningVerify = checksum.RunningChecksum()

        chunks = chunkRanges(start, stop, verifyChunkSize)
        for i, (cs, ce) in enumerate(chunks):
            testpath = self._mktmp('.whipper.test.wav')
            verifypath = self._mktmp('.whipper.verify.wav')
            self._chunkPaths.append((testpath, verifypath))
            if len(chunks) > 1:
                chunkWhat = '%s (frames %d-%d)' % (what, cs, ce)
            else:
                chunkWhat = what
            t_read = ReadTrackTask(testpath, table, cs, ce, overread,
                                   offset=offset, device=device,
                                   what=chunkWhat)
            t_crcTest = checksum.CRC32Task(testpath, chain=self._runningTest)
            t_verify = ReadTrackTask(verifypath, table, cs, ce, overread,
                                     offset=offset, device=device,
                                     action="Verifying", what=chunkWhat)
            t_crcVerify = checksum.CRC32Task(verifypath,
                                             chain=self._runningVerify)
            t_append = VerifyChunkTask(t_crcTest, t_crcVerify, testpath,
                                       verifypath, tmppath, cs, ce,
                                       first=(i == 0),
                                       totalDataBytes=(
                                           (stop - start + 1) *
                                           common.BYTES_PER_FRAME))
            self._readTasks.append(t_read)
            self._verifyTasks.append(t_verify)
            self.tasks += [t_read, t_crcTest, t_verify, t_crcVerify, t_append]

        # encode to the final path + '.part'
        try:
            tmpoutpath = path + '.part'
            open(tmpoutpath, 'wb').close()
        except IOError as e:
            if errno.ENAMETOOLONG != e.errno:
                raise
            path = common.truncate_filename(common.shrinkPath(path))
            tmpoutpath = common.truncate_filename(path + '.part')
            open(tmpoutpath, 'wb').close()
        self._tmppath = tmpoutpath
        self.path = path

        from whipper.common import encode

        self.tasks.append(encode.FlacEncodeTask(tmppath, tmpoutpath))

        # MerlijnWajer: XXX: We run the CRC32Task on the wav file, because it's
        # in general stupid to run the CRC32 on the flac file since it already
        # has --verify. We should just get rid of this CRC32 step.
        # make sure our encoding is accurate
        self._encodeCrcTask = checksum.CRC32Task(tmppath)
        self.tasks.append(self._encodeCrcTask)
        self._peakTask = encode.SoxPeakTask(tmppath)
        self.tasks.append(self._peakTask)

        # TODO: Move tagging and embed picture outside of cdparanoia
        self.tasks.append(encode.TaggingTask(tmpoutpath, taglist))
        self.tasks.append(encode.EmbedPictureTask(tmpoutpath, coverArtPath))

        self.checksum = None

    @staticmethod
    def _mktmp(suffix):
        fd, path = tempfile.mkstemp(suffix=suffix)
        os.fchmod(fd, 0o644)
        os.close(fd)
        return path

    def stop(self):
        # FIXME: maybe this kind of try-wrapping to make sure
        # we chain up should be handled by a parent class function ?
        try:
            # clean up chunk temp files, whether consumed or not
            for testpath, verifypath in self._chunkPaths:
                for p in (testpath, verifypath):
                    if p and os.path.exists(p):
                        os.unlink(p)

            if not self.exception:
                totalFrames = self._stop - self._start + 1
                testReads = sum(t._parser.reads for t in self._readTasks)
                verifyReads = sum(t._parser.reads for t in self._verifyTasks)
                self.quality = max(min(totalFrames * 2.0 / testReads, 1.0),
                                   min(totalFrames * 2.0 / verifyReads, 1.0))
                self.peak = self._peakTask.peak
                logger.debug('peak: %r', self.peak)
                self.testduration = sum(t.duration for t in self._readTasks)
                self.copyduration = sum(t.duration for t in self._verifyTasks)
                self.testspeed = (totalFrames / 75.0) / self.testduration
                self.copyspeed = (totalFrames / 75.0) / self.copyduration

                self.testchecksum = c1 = self._runningTest.value & 0xffffffff
                self.copychecksum = c2 = self._runningVerify.value & 0xffffffff
                if c1 == c2:
                    logger.info('checksums match, %08x', c1)
                    self.checksum = self.testchecksum
                else:
                    # FIXME: detect this before encoding
                    logger.info('checksums do not match, %08x %08x',
                                c1, c2)
                    self.exception = ChecksumException(
                        'read and verify failed: test checksum')

                if self._encodeCrcTask.checksum != self.checksum:
                    self.exception = ChecksumException(
                        'Encoding failed, checksum does not match')

                # delete the unencoded file
                os.unlink(self._tmpwavpath)

                if not self.exception:
                    try:
                        logger.debug('moving to final path %r', self.path)
                        shutil.move(self._tmppath, self.path)
                    # FIXME: catching too general exception (Exception)
                    except Exception as e:
                        logger.debug('exception while moving to final '
                                     'path %r: %s', self.path, e)
                        self.exception = e
                else:
                    os.unlink(self._tmppath)
            else:
                logger.debug('stop: exception %r', self.exception)
                for p in (self._tmpwavpath, self._tmppath):
                    if p and os.path.exists(p):
                        os.unlink(p)
        # FIXME: catching too general exception (Exception)
        except Exception as e:
            print('WARNING: unhandled exception %r' % (e, ))

        task.MultiSeparateTask.stop(self)


_VERSION_RE = re.compile(
    "^cdparanoia (?P<version>.+) release (?P<release>.+)")


def getCdParanoiaVersion():
    getter = common.VersionGetter('cd-paranoia',
                                  ["cd-paranoia", "-V"],
                                  _VERSION_RE,
                                  "%(version)s %(release)s")

    return getter.get()


_OK_RE = re.compile(r'Drive tests OK with Paranoia.')
_WARNING_RE = re.compile(r'WARNING! PARANOIA MAY NOT BE')
_ABORTING_RE = re.compile(r'aborting test\.')


class AnalyzeTask(ctask.PopenTask):

    logCategory = 'AnalyzeTask'
    description = 'Analyzing drive caching behaviour'

    defeatsCache = None

    cwd = None

    _output = []

    def __init__(self, device=None):
        # cdparanoia -A *always* writes cdparanoia.log
        self.cwd = tempfile.mkdtemp(suffix='.whipper.cache')
        self.command = ['cd-paranoia', '-A']
        if device:
            self.command += ['-d', device]

    def commandMissing(self):
        raise common.MissingDependencyException('cd-paranoia')

    def readbyteserr(self, bytes_stderr):
        self._output.append(bytes_stderr)

    def done(self):
        if self.cwd:
            shutil.rmtree(self.cwd)
        output = "".join(o.decode() for o in self._output)
        m = _OK_RE.search(output)
        self.defeatsCache = bool(m)

    def failed(self):
        # cdparanoia exits with return code 1 if it can't determine
        # whether it can defeat the audio cache
        output = "".join(o.decode() for o in self._output)
        m = _WARNING_RE.search(output)
        if m or _ABORTING_RE.search(output):
            self.defeatsCache = False
        if self.cwd:
            shutil.rmtree(self.cwd)
