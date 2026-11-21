import hashlib
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from edition import RevisionCatalog, build_successor_seed, freeze
from intake import Correction, DeviceSegment, DraftLayout, DraftStore, correct, missing_positions, open_and_load, open_successor


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'source.sqlite'
        self.layout = DraftLayout('tensile', 2, 1)
        self._connect()

    def _connect(self):
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.drafts = DraftStore(self.db)
        self.catalog = RevisionCatalog(self.db)

    def _filled(self):
        return open_and_load(self.drafts, self.layout, (
            DeviceSegment('late-slot', 1, b'\x01'), DeviceSegment('early-slot', 0, b'\x01'),
        ))

    def test_freeze_out_of_order_segments(self):
        draft = self._filled()
        frozen = freeze(self.drafts, self.catalog, draft.draft_id, 'R1')
        self.assertEqual(frozen.bodies, (b'\x01', b'\x01'))
        self.assertEqual(tuple(entry.position for entry in frozen.directory.entries), (0, 1))
        self.assertEqual(tuple(entry.origins[0].reference for entry in frozen.directory.entries), ('early-slot', 'late-slot'))
        self.assertEqual(tuple(entry.digest for entry in frozen.directory.entries), (hashlib.sha256(b'\x01').hexdigest(),) * 2)
        self.assertEqual(frozen.directory.total_bytes, 2)
        self.assertEqual(self.catalog.get('tensile', 'R1'), frozen)

    def test_segment_replay_and_conflict(self):
        draft = self._filled()
        self.assertEqual(open_and_load(self.drafts, self.layout, (draft.segments[0],) * 2), draft)
        for conflict in (DeviceSegment('late-slot', 0, b'\x01'), DeviceSegment('late-slot', 1, b'\x09')):
            with self.subTest(conflict=conflict):
                with self.assertRaisesRegex(ValueError, 'segment identity conflict'):
                    open_and_load(self.drafts, self.layout, (DeviceSegment('new-evidence', 0, b'\x02'), conflict))
                self.assertEqual(self.drafts.get(draft.draft_id), draft)
        expanded = open_and_load(self.drafts, self.layout, (DeviceSegment('new-evidence', 0, b'\x02'),))
        self.assertEqual(expanded.segments[-1], DeviceSegment('new-evidence', 0, b'\x02'))
        self.assertEqual(expanded.blocks, draft.blocks)
        self.assertEqual(len(expanded.segments), 3)

    def test_invalid_submission(self):
        for layout in (DraftLayout('invalid', -1, 1), DraftLayout('invalid', 2, 0), DraftLayout('invalid', True, 1)):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                open_and_load(self.drafts, layout)
        good = DeviceSegment('good', 0, b'\x01')
        for bad in (DeviceSegment('too-wide', 1, b'\x01\x02'), DeviceSegment('outside', 2, b'\x02')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                open_and_load(self.drafts, self.layout, (good, bad))
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM drafts').fetchone()[0], 0)
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM current_drafts').fetchone()[0], 0)
        draft = open_and_load(self.drafts, self.layout, (good,))
        with self.assertRaisesRegex(ValueError, 'layout conflicts'):
            open_and_load(self.drafts, DraftLayout('tensile', 3, 1), (DeviceSegment('other', 1, b'\x02'),))
        with self.assertRaises(ValueError):
            open_and_load(self.drafts, self.layout, (DeviceSegment('new', 1, b'\x02'), DeviceSegment('bad', -1, b'\x02')))
        self.assertEqual(self.drafts.get(draft.draft_id), draft)

    def test_missing_slot_blocks_freeze(self):
        draft = open_and_load(self.drafts, self.layout, (DeviceSegment('observed', 0, b'\x01'),))
        self.assertEqual(missing_positions(self.layout, draft.blocks), (1,))
        with self.assertRaisesRegex(ValueError, 'missing positions'):
            freeze(self.drafts, self.catalog, draft.draft_id, 'R1')
        self.assertIsNone(self.catalog.get('tensile', 'R1'))
        self.assertEqual(self.drafts.get(draft.draft_id), draft)
        complete = open_and_load(self.drafts, self.layout, (DeviceSegment('real-zero', 1, b'\x00'),))
        frozen = freeze(self.drafts, self.catalog, complete.draft_id, 'R1')
        self.assertEqual(frozen.bodies, (b'\x01', b'\x00'))
        self.assertEqual(frozen.directory.entries[1].origins[0].reference, 'real-zero')
        self.assertEqual(frozen.directory.total_bytes, 2)

    def test_revision_conflict(self):
        draft = self._filled()
        original = freeze(self.drafts, self.catalog, draft.draft_id, 'R1')
        amended = correct(self.drafts, draft.draft_id, Correction(0, b'\x08', 'instrument-note'))
        with self.assertRaisesRegex(ValueError, 'already frozen'):
            freeze(self.drafts, self.catalog, amended.draft_id, 'R1')
        self.assertEqual(self.catalog.get('tensile', 'R1'), original)
        self.assertEqual(self.drafts.get(amended.draft_id), amended)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM frozen_revisions').fetchone()[0], 1)

    def test_correction_keeps_parent(self):
        base_draft = self._filled()
        parent = freeze(self.drafts, self.catalog, base_draft.draft_id, 'R1')
        seed = build_successor_seed(self.catalog, 'tensile', 'R1')
        child = open_successor(self.drafts, seed)
        self.assertNotEqual(child.draft_id, base_draft.draft_id)
        self.assertEqual(child.parent_revision, 'R1')
        amended = correct(self.drafts, child.draft_id, Correction(1, b'\x02', 'quality-7'))
        self.assertEqual(open_and_load(self.drafts, self.layout), amended)
        self.db.close()
        self._connect()
        reopened = open_and_load(self.drafts, self.layout)
        self.assertEqual(reopened, amended)
        self.assertEqual(open_successor(self.drafts, build_successor_seed(self.catalog, 'tensile', 'R1')), amended)
        child_frozen = freeze(self.drafts, self.catalog, reopened.draft_id, 'R2')
        self.assertEqual(self.catalog.get('tensile', 'R1'), parent)
        self.assertEqual(parent.bodies, (b'\x01', b'\x01'))
        self.assertEqual(child_frozen.bodies, (b'\x01', b'\x02'))
        self.assertEqual(tuple(origin.reference for origin in parent.directory.entries[1].origins), ('late-slot',))
        self.assertEqual(tuple(origin.reference for origin in child_frozen.directory.entries[1].origins), ('late-slot', 'R1', 'quality-7'))
        self.assertEqual(tuple(origin.reference for origin in child_frozen.directory.entries[0].origins), ('early-slot', 'R1'))

    def test_invalid_correction(self):
        draft = open_and_load(self.drafts, self.layout, (DeviceSegment('present', 0, b'\x01'),))
        for change in (Correction(2, b'\x02', 'outside'), Correction(1, b'\x02', 'absent'), Correction(0, b'', 'width')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                correct(self.drafts, draft.draft_id, change)
        self.assertEqual(self.drafts.get(draft.draft_id), draft)
        with self.assertRaises(LookupError):
            build_successor_seed(self.catalog, 'other-volume', 'R1')
        complete = self._filled()
        frozen = freeze(self.drafts, self.catalog, complete.draft_id, 'R1')
        with self.assertRaisesRegex(ValueError, 'editable draft ID'):
            correct(self.drafts, frozen.directory.revision, Correction(0, b'\x02', 'frozen'))
        self.assertEqual(self.catalog.get('tensile', 'R1'), frozen)
