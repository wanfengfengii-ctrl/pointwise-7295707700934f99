import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from edition import RevisionCatalog, freeze
from edition.directory import build_directory
from intake import DeviceSegment, DraftLayout, DraftStore, open_and_load
from intake.placement import BlockOrigin, PlacedBlock
from reconcile import StatementArchive, read_history, reconcile
from shipment import IssueHistory, encode_literal_packet, issue
from station import ReceiptStore, StationMaterial, accept


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source_path = root / 'source.sqlite'
        self.station_path = root / 'station.sqlite'
        self.source = sqlite3.connect(self.source_path)
        self.addCleanup(self.source.close)
        self.drafts = DraftStore(self.source)
        self.catalog = RevisionCatalog(self.source)
        self.issues = IssueHistory(self.source)
        self._connect_station()

    def _connect_station(self):
        self.db = sqlite3.connect(self.station_path)
        self.addCleanup(self.db.close)
        self.material = StationMaterial(self.db)
        self.receipts = ReceiptStore(self.db)
        self.archive = StatementArchive(self.db)

    def _freeze(self, volume, bodies, width=1):
        layout = DraftLayout(volume, len(bodies), width)
        segments = tuple(DeviceSegment(f'device-{position}', position, data) for position, data in enumerate(bodies))
        draft = open_and_load(self.drafts, layout, segments)
        return freeze(self.drafts, self.catalog, draft.draft_id, 'R1').directory

    def _issue(self, directory, positions):
        return issue(self.catalog, self.issues, directory.volume, directory.revision, 'A', positions)

    def _accept(self, packet):
        return accept(self.material, self.receipts, 'A', packet)

    def _report(self, directory):
        return reconcile(self.material, self.archive, 'A', directory)

    def test_commit_failure_keeps_previous_coverage(self):
        directory = self._freeze('fatigue', (b'\x01', b'\x02', b'\x03'))
        old_receipt = self._accept(self._issue(directory, [0]))
        incoming = self._issue(directory, [1, 2])
        self.db.execute('''
            CREATE TRIGGER reject_second_new_position BEFORE INSERT ON station_blocks
            WHEN NEW.position = 2
            BEGIN SELECT RAISE(ABORT, 'desk ledger refuses position 2'); END
        ''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'desk ledger refuses position 2'):
            self._accept(incoming)
        self.assertIsNone(self.receipts.find_packet('A', incoming.packet_id))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM station_receipts').fetchone()[0], 1)
        self.assertEqual(tuple(self.material.read_positions('A', directory)), (0,))
        before = self._report(directory)
        self.assertEqual((before.received, before.missing, before.received_bytes), ((0,), (1, 2), 1))
        self.assertEqual(before.lines[0].first_receipt_id, old_receipt.receipt_id)
        self.db.execute('DROP TRIGGER reject_second_new_position')
        accepted = self._accept(incoming)
        self.assertEqual(tuple(line.data for line in accepted.lines), (b'\x02', b'\x03'))
        self.assertEqual(tuple(line.first_receipt_id for line in accepted.lines), (accepted.receipt_id,) * 2)
        self.assertEqual(self._accept(incoming), accepted)
        after = self._report(directory)
        self.assertEqual((after.received, after.missing, after.received_bytes), ((0, 1, 2), (), 3))
        self.assertEqual(read_history(self.receipts, self.archive, 'A', before.report_id), before)
        print('POINTWISE_SCENARIO ' + json.dumps({
            'case_id': 'v7',
            'input': {'old_positions': [0], 'arrival': [1, 2], 'width': 1, 'rejected_insert_position': 2},
            'expected': {'received': [0], 'missing': [1, 2], 'bytes': 1},
            'actual': {'received': before.received, 'missing': before.missing, 'bytes': before.received_bytes},
            'downstream_expected': {'received': [0, 1, 2], 'missing': [], 'bytes': 3},
            'downstream_actual': {'received': after.received, 'missing': after.missing, 'bytes': after.received_bytes},
            'code_paths': ['station/commit.py', 'station/material.py', 'station/receipts.py', 'reconcile/closeout.py'],
            'oracle': 'One old one-byte position survives the rejected transaction. After retry the three distinct positions count one byte each.',
        }, sort_keys=True))

    def test_first_acceptance_failure_rolls_back_directory(self):
        directory = self._freeze('initial', (b'\x01', b'\x02'))
        packet = self._issue(directory, [0, 1])
        self.db.execute('''
            CREATE TRIGGER reject_receipt_snapshot BEFORE UPDATE OF snapshot ON station_receipts
            BEGIN SELECT RAISE(ABORT, 'desk ledger refuses receipt snapshot'); END
        ''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'refuses receipt snapshot'):
            self._accept(packet)
        self.assertIsNone(self.material.get_directory('A', directory.volume, directory.revision))
        self.assertEqual(self.material.read_positions('A', directory), {})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM station_receipts').fetchone()[0], 0)
        self.assertIsNone(read_history(self.receipts, self.archive, 'A', 'R-1'))
        with self.assertRaises(LookupError):
            self._report(directory)
        self.db.execute('DROP TRIGGER reject_receipt_snapshot')
        self.assertEqual(tuple(line.position for line in self._accept(packet).lines), (0, 1))

    def test_closed_source_preserves_partial_and_full_reports(self):
        directory = self._freeze('night', (b'\x01', b'\x02'))
        first_packet = self._issue(directory, [1])
        first_receipt = self._accept(encode_literal_packet(first_packet))
        partial = self._report(directory)
        self.assertEqual((partial.received, partial.missing, partial.received_bytes), ((1,), (0,), 1))
        self.assertEqual(partial.lines[0].origins[0].reference, 'device-1')
        self.assertEqual(partial.lines[0].first_receipt_id, first_receipt.receipt_id)
        top_up = self._issue(directory, [1, *partial.missing])
        second_receipt = self._accept(top_up)
        self.assertEqual(tuple(line.position for line in second_receipt.lines), (1, 0))
        self.assertEqual(tuple(line.first_receipt_id for line in second_receipt.lines), (first_receipt.receipt_id, second_receipt.receipt_id))
        complete = self._report(directory)
        self.assertEqual((complete.received, complete.missing, complete.received_bytes), ((0, 1), (), 2))
        self.assertNotEqual(partial.report_id, complete.report_id)
        self.assertEqual(tuple(line.origins[0].reference for line in complete.lines), ('device-0', 'device-1'))
        self.source.close()
        self.source_path.rename(self.source_path.with_suffix('.offline'))
        self.db.close()
        self._connect_station()
        for value, identifier in ((partial, partial.report_id), (complete, complete.report_id),
                                  (first_receipt, first_receipt.receipt_id), (second_receipt, second_receipt.receipt_id)):
            with self.subTest(identifier=identifier):
                self.assertEqual(read_history(self.receipts, self.archive, 'A', identifier), value)
                self.assertIsNone(read_history(self.receipts, self.archive, 'B', identifier))
        for identifier in ('S-900', 'R-900', first_packet.packet_id, 'unknown'):
            self.assertIsNone(read_history(self.receipts, self.archive, 'A', identifier))
        self.assertEqual(self._accept(top_up), second_receipt)

    def test_unknown_or_conflicting_directory(self):
        directory = self._freeze('directory', (b'\x01',))
        with self.assertRaises(LookupError):
            self._report(directory)
        self._accept(self._issue(directory, []))
        alternate = build_directory(directory.layout, directory.revision, (
            PlacedBlock(0, b'\x01', (BlockOrigin('device', 'different-evidence'),)),
        ))
        with self.assertRaisesRegex(ValueError, 'conflicts with station directory'):
            self._report(alternate)
        with self.assertRaises(LookupError):
            reconcile(self.material, self.archive, 'B', directory)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM station_statements').fetchone()[0], 0)
        self.assertEqual(self._report(directory).missing, (0,))

    def test_empty_coverage_and_zero_slot_volume(self):
        directory = self._freeze('unreceived', (b'AB', b'CD'), width=2)
        first = self._accept(self._issue(directory, []))
        self.assertEqual(first.lines, ())
        report = self._report(directory)
        self.assertEqual((report.received, report.missing, report.received_bytes), ((), (0, 1), 0))
        self._accept(self._issue(directory, [1]))
        self.assertEqual(self._report(directory).received_bytes, 2)
        empty = self._freeze('empty', (), width=2)
        packet = issue(self.catalog, self.issues, empty.volume, empty.revision, 'A')
        receipt = self._accept(packet)
        self.assertEqual(receipt.lines, ())
        closed = self._report(empty)
        self.assertEqual((closed.received, closed.missing, closed.received_bytes, empty.total_bytes), ((), (), 0, 0))
        self.assertEqual(read_history(self.receipts, self.archive, 'A', closed.report_id), closed)

    def test_failed_closeout_preserves_history(self):
        directory = self._freeze('archive', (b'\x01',))
        self._accept(self._issue(directory, []))
        original = self._report(directory)
        self.db.execute('''
            CREATE TRIGGER reject_statement BEFORE UPDATE OF snapshot ON station_statements
            BEGIN SELECT RAISE(ABORT, 'report archive refuses snapshot'); END
        ''')
        self._accept(self._issue(directory, [0]))
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'report archive refuses snapshot'):
            self._report(directory)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM station_statements').fetchone()[0], 1)
        self.assertEqual(self.archive.get('A', original.report_id), original)
        self.db.execute('DROP TRIGGER reject_statement')
        self.assertEqual(self._report(directory).received, (0,))
