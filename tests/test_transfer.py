import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from edition import RevisionCatalog, build_successor_seed, freeze
from edition.directory import build_directory
from intake import Correction, DeviceSegment, DraftLayout, DraftStore, correct, open_and_load, open_successor
from intake.placement import BlockOrigin, PlacedBlock
from shipment import IssueHistory, decode_literal_packet, encode_literal_packet, issue
from station import ReceiptStore, StationMaterial, accept
from tests.broadcast import BENCH_FRAME_BUDGET, broadcast


class TransferTests(unittest.TestCase):
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
        self.layout = DraftLayout('pressure', 2, 1)
        draft = open_and_load(self.drafts, self.layout, (DeviceSegment('x', 0, b'\x01'), DeviceSegment('y', 1, b'\x02')))
        self.frozen = freeze(self.drafts, self.catalog, draft.draft_id, 'R1')
        self._connect_station()

    def _connect_station(self):
        self.db = sqlite3.connect(self.station_path)
        self.addCleanup(self.db.close)
        self.material = StationMaterial(self.db)
        self.receipts = ReceiptStore(self.db)

    def _issue(self, positions):
        return issue(self.catalog, self.issues, 'pressure', 'R1', 'A', positions)

    def _count(self, table):
        statements = {
            'issued': (self.source, 'SELECT COUNT(*) FROM issued_packets'),
            'receipts': (self.db, 'SELECT COUNT(*) FROM station_receipts'),
            'directories': (self.db, 'SELECT COUNT(*) FROM station_directories'),
        }
        connection, sql = statements[table]
        return connection.execute(sql).fetchone()[0]

    def test_issue_rejects_unknown_position(self):
        valid = self._issue([1])
        with self.assertRaisesRegex(ValueError, 'position outside layout'):
            self._issue([2])
        self.assertEqual(self.issues.get(valid.packet_id), valid)
        self.assertEqual(self._count('issued'), 1)

    def test_mixed_selection_and_invalid_revision(self):
        valid = self._issue([1])
        for positions in ([1, 2], [1, 1], [True], {0, 1}):
            with self.subTest(positions=positions), self.assertRaises(ValueError):
                self._issue(positions)
        with self.assertRaises(LookupError):
            issue(self.catalog, self.issues, 'pressure', 'unknown', 'A', [0])
        self.assertEqual(self._count('issued'), 1)
        self.assertEqual(self.issues.get(valid.packet_id), valid)

    def test_full_volume(self):
        full = issue(self.catalog, self.issues, 'pressure', 'R1', 'B')
        self.assertEqual(full.station, 'B')
        self.assertEqual(tuple((block.position, block.data) for block in full.blocks), ((0, b'\x01'), (1, b'\x02')))
        self.assertEqual(full.directory, self.frozen.directory)
        self.source.close()
        with sqlite3.connect(self.source_path) as reopened:
            self.addCleanup(reopened.close)
            self.assertEqual(IssueHistory(reopened).get(full.packet_id), full)

    def test_reordered_overlapping_literal_deliveries(self):
        original = self._issue([1, 0])
        overlap = self._issue([0])
        wire = encode_literal_packet(original)
        self.assertEqual(decode_literal_packet(wire), original)
        first = accept(self.material, self.receipts, 'A', wire)
        second = accept(self.material, self.receipts, 'A', overlap)
        self.assertEqual(tuple((line.position, line.data) for line in first.lines), ((1, b'\x02'), (0, b'\x01')))
        self.assertEqual(tuple(line.first_receipt_id for line in first.lines), (first.receipt_id,) * 2)
        self.assertNotEqual(second.receipt_id, first.receipt_id)
        self.assertEqual(second.lines, (first.lines[1],))
        ledger = self.material.read_positions('A', original.directory)
        self.assertEqual(tuple(ledger), (0, 1))
        self.assertTrue(all(block.first_receipt_id == first.receipt_id for block in ledger.values()))
        self.db.close()
        self.source.close()
        self._connect_station()
        spaced_wire = json.dumps(json.loads(wire), indent=2).encode('utf-8')
        self.assertEqual(accept(self.material, self.receipts, 'A', spaced_wire), first)
        self.assertEqual(self.receipts.get('A', first.receipt_id), first)
        self.assertEqual(self._count('receipts'), 2)
        self.assertEqual(self.material.read_positions('A', original.directory), ledger)

    def test_empty_selection(self):
        packet = self._issue([])
        self.assertEqual(packet.blocks, ())
        receipt = accept(self.material, self.receipts, 'A', encode_literal_packet(packet))
        self.assertEqual(receipt.lines, ())
        self.assertEqual(self.material.get_directory('A', 'pressure', 'R1'), self.frozen.directory)
        self.assertEqual(self.material.read_positions('A', self.frozen.directory), {})
        self.assertEqual(accept(self.material, self.receipts, 'A', packet), receipt)
        self.assertEqual(self._count('receipts'), 1)

    def test_revision_partition(self):
        child = open_successor(self.drafts, build_successor_seed(self.catalog, 'pressure', 'R1'))
        correct(self.drafts, child.draft_id, Correction(1, b'\x03', 'quality-note'))
        new = freeze(self.drafts, self.catalog, child.draft_id, 'R2')
        old_packet = self._issue([0])
        new_packet = issue(self.catalog, self.issues, 'pressure', 'R2', 'A', [0])
        old_receipt = accept(self.material, self.receipts, 'A', old_packet)
        new_receipt = accept(self.material, self.receipts, 'A', new_packet)
        self.assertEqual(new_receipt.lines[0].data, old_receipt.lines[0].data)
        self.assertEqual(new_receipt.lines[0].first_receipt_id, new_receipt.receipt_id)
        self.assertEqual(old_receipt.lines[0].first_receipt_id, old_receipt.receipt_id)
        self.assertNotEqual(new_receipt.receipt_id, old_receipt.receipt_id)
        self.assertEqual(tuple(origin.reference for origin in new_receipt.lines[0].origins), ('x', 'R1'))
        self.assertEqual(tuple(self.material.read_positions('A', new.directory)), (0,))
        self.assertEqual(accept(self.material, self.receipts, 'A', old_packet), old_receipt)
        self.assertEqual(accept(self.material, self.receipts, 'A', new_packet), new_receipt)

    def test_corrupted_second_block_rejects_whole_packet(self):
        packet = self._issue([0, 1])
        corrupt = replace(packet, blocks=(packet.blocks[0], replace(packet.blocks[1], data=b'\xfe')))
        with self.assertRaisesRegex(ValueError, 'digest mismatch at position 1'):
            accept(self.material, self.receipts, 'A', encode_literal_packet(corrupt))
        self.assertIsNone(self.receipts.find_packet('A', packet.packet_id))
        self.assertEqual(self._count('receipts'), 0)
        self.assertEqual(self._count('directories'), 0)
        self.assertEqual(self.material.read_positions('A', packet.directory), {})
        accepted = accept(self.material, self.receipts, 'A', packet)
        self.assertEqual(tuple(line.data for line in accepted.lines), (b'\x01', b'\x02'))

    def test_station_and_directory(self):
        first = self._issue([0])
        with self.assertRaisesRegex(ValueError, 'different station'):
            accept(self.material, self.receipts, 'B', first)
        self.assertEqual(self._count('directories'), 0)
        valid = accept(self.material, self.receipts, 'A', first)
        alternate = build_directory(self.layout, 'R1', (
            PlacedBlock(0, b'\x01', (BlockOrigin('device', 'alternate-origin'),)),
            PlacedBlock(1, b'\x02', (BlockOrigin('device', 'y'),)),
        ))
        second = self._issue([1])
        with self.assertRaisesRegex(ValueError, 'conflicts with station directory'):
            accept(self.material, self.receipts, 'A', replace(second, directory=alternate))
        wrong_revision = replace(second.directory, revision='R2')
        with self.assertRaisesRegex(ValueError, 'directory digest mismatch'):
            accept(self.material, self.receipts, 'A', replace(second, directory=wrong_revision))
        self.assertEqual(self.material.get_directory('A', 'pressure', 'R1'), first.directory)
        self.assertEqual(self.receipts.get('A', valid.receipt_id), valid)
        self.assertEqual(tuple(self.material.read_positions('A', first.directory)), (0,))
        self.assertEqual(accept(self.material, self.receipts, 'A', second).lines[0].position, 1)

    def test_changed_packet_identity(self):
        packet = self._issue([1, 0])
        first = accept(self.material, self.receipts, 'A', packet)
        changed = replace(packet, blocks=(packet.blocks[1],))
        with self.assertRaisesRegex(ValueError, 'different content'):
            accept(self.material, self.receipts, 'A', changed)
        self.assertEqual(self.receipts.get('A', first.receipt_id), first)
        self.assertEqual(self._count('receipts'), 1)

    def test_invalid_wire_and_duplicate_positions(self):
        packet = self._issue([0])
        payload = json.loads(encode_literal_packet(packet))
        for version in (True, 2):
            with self.subTest(version=version), self.assertRaises(ValueError):
                accept(self.material, self.receipts, 'A', json.dumps(dict(payload, version=version)).encode())
        with self.assertRaises(ValueError):
            accept(self.material, self.receipts, 'A', b'{"version":1,"version":1}')
        with self.assertRaisesRegex(ValueError, 'duplicate packet position'):
            accept(self.material, self.receipts, 'A', replace(packet, blocks=packet.blocks * 2))
        self.assertEqual(self._count('receipts'), 0)
        self.assertEqual(self._count('directories'), 0)

    def test_one_frame_literal_bench(self):
        packet = self._issue([1])
        payload = encode_literal_packet(packet)
        delivered = broadcast(payload, len(packet.blocks))
        self.assertEqual(delivered.frame_count, BENCH_FRAME_BUDGET)
        self.assertIs(delivered.copies[0], delivered.copies[1])
        self.assertEqual(accept(self.material, self.receipts, 'A', delivered.copies[0]).lines[0].data, b'\x02')
        with self.assertRaisesRegex(ValueError, 'different station'):
            accept(self.material, self.receipts, 'B', delivered.copies[1])
