# -*- Mode: Python; test-case-name: whipper.test.test_command_cd -*-
# vi:si:et:sw=4:sts=4:ts=4

import os
import shutil
import tempfile
from types import SimpleNamespace
from unittest import mock

from whipper.command.cd import Rip
from whipper.result import result
from whipper.test import common


def _make_rip(outdir, skip_existing):
    """Build a Rip command with mocked drive/table, ready for doCommand()."""
    rip = object.__new__(Rip)
    rip.options = SimpleNamespace(
        working_directory=None,
        output_directory=outdir,
        offset=0,
        overread=False,
        logger='whipper',
        cover_art=None,
        track_template='%t',
        disc_template='disc',
        max_retries=1,
        keep_going=False,
        skip_existing=skip_existing,
        verify_chunk_size=0,
    )
    rip.program = mock.Mock()
    rip.program.result = result.RipResult()
    rip.program.getHTOA.return_value = None

    def getPath(outdir, template, mbdiscid, metadata, track_number=None):
        if track_number is not None:
            return os.path.join(outdir, 'track%02d' % track_number)
        return os.path.join(outdir, 'disc')

    rip.program.getPath.side_effect = getPath
    rip.program.getTagList.return_value = {}
    rip.runner = mock.Mock()
    rip.itable = mock.Mock()
    track = mock.Mock()
    track.audio = True
    track.isrc = None
    rip.itable.tracks = [track]
    rip.mbdiscid = 'discid'
    rip.device = '/dev/null'
    rip.logger = None
    rip.coverArtPath = None
    rip.skipped_tracks = []
    return rip


class SkipExistingTestCase(common.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(suffix='.whipper.test.cd')
        self.track_path = os.path.join(self.dir, 'track01.flac')
        with open(self.track_path, 'wb') as f:
            f.write(b'previously ripped')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def testExistingTrackIsVerifiedAndRerippedByDefault(self):
        rip = _make_rip(self.dir, skip_existing=False)
        # pretend the on-disk file does not match the stored crc
        rip.program.verifyTrack.return_value = False

        rip.doCommand()

        self.assertTrue(rip.program.verifyTrack.called)
        # failed verification means the file was deleted for re-ripping
        self.assertFalse(os.path.exists(self.track_path))

    def testSkipExistingKeepsFileWithoutVerifying(self):
        rip = _make_rip(self.dir, skip_existing=True)
        rip.program.verifyTrack.return_value = False

        rip.doCommand()

        self.assertFalse(rip.program.verifyTrack.called)
        self.assertFalse(rip.program.ripTrack.called)
        self.assertTrue(os.path.exists(self.track_path))

    def testRipOptionsArePassedToRipTrack(self):
        # no existing file, so the track gets ripped
        os.unlink(self.track_path)
        rip = _make_rip(self.dir, skip_existing=False)
        rip.options.verify_chunk_size = 150
        rip.options.max_retries = 7

        rip.doCommand()

        self.assertTrue(rip.program.ripTrack.called)
        _, kwargs = rip.program.ripTrack.call_args
        self.assertEqual(kwargs['verifyChunkSize'], 150)
        # -r/--max-retries also bounds the per-chunk verify retries
        self.assertEqual(kwargs['maxChunkAttempts'], 7)

    def testFinishedRipDirRejectedByDefault(self):
        # a .log next to the disc file marks the rip as finished
        with open(os.path.join(self.dir, 'disc.log'), 'w') as f:
            f.write('log')
        rip = _make_rip(self.dir, skip_existing=False)
        self.assertRaises(RuntimeError, rip.doCommand)

    def testSkipExistingAllowsFinishedRipDir(self):
        with open(os.path.join(self.dir, 'disc.log'), 'w') as f:
            f.write('log')
        rip = _make_rip(self.dir, skip_existing=True)

        rip.doCommand()

        self.assertFalse(rip.program.verifyTrack.called)
        self.assertTrue(os.path.exists(self.track_path))
