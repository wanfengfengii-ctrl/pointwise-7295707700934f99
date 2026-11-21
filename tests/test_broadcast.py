import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from edition import RevisionCatalog, build_successor_seed, freeze
from edition.directory import build_directory, directory_to_data
from intake.layout import DraftLayout
from intake import DeviceSegment, DraftLayout, DraftStore, open_and_load, open_successor
from intake.placement import BlockOrigin, PlacedBlock
from reconcile import StatementArchive, reconcile
from shipment import (
    IssueHistory,
    decode_broadcast_packet,
    decode_literal_packet,
    encode_broadcast_packet,
    encode_literal_packet,
    issue,
    plan_broadcast,
)
from station import ReceiptStore, StationMaterial, accept, accept_broadcast


class BroadcastTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = sqlite3.connect(self.root / 'source.sqlite')
        self.addCleanup(self.source.close)
        self.drafts = DraftStore(self.source)
        self.catalog = RevisionCatalog(self.source)
        self.issues = IssueHistory(self.source)
        self.desk_paths = {name: self.root / f'desk-{name}.sqlite' for name in ('A', 'B')}
        self.desks = {}

    def _leave(self):
        for connection in self.desks.values():
            connection.close()
        self.desks = {}

    def _freeze(self, volume, bodies, revision='R1', width=1):
        layout = DraftLayout(volume, len(bodies), width)
        segments = tuple(
            DeviceSegment(f'device-{position}', position, data) for position, data in enumerate(bodies)
        )
        draft = open_and_load(self.drafts, layout, segments)
        return freeze(self.drafts, self.catalog, draft.draft_id, revision).directory

    def _desk(self, name):
        if name not in self.desks:
            self.desks[name] = sqlite3.connect(self.desk_paths[name])
        connection = self.desks[name]
        return StationMaterial(connection), ReceiptStore(connection), StatementArchive(connection)

    def _preload(self, name, directory, positions):
        material, receipts, _ = self._desk(name)
        accept(material, receipts, name, issue(
            self.catalog, self.issues, directory.volume, directory.revision, name, list(positions)
        ))

    def _plan(self, directory, a=((), ()), b=((), ()), budget=1):
        return plan_broadcast(
            self.catalog, self.issues, directory.volume, directory.revision,
            'A', list(a[0]), list(a[1]), 'B', list(b[0]), list(b[1]), budget,
        )

    # --- 发行 -------------------------------------------------------------

    def test_complementary_one_frame(self):
        directory = self._freeze('bench', (b'\x01', b'\x02'))
        wire = self._plan(directory, ((0,), (1,)), ((1,), (0,)), budget=1)
        self.assertIsInstance(wire, bytes)
        packet = decode_broadcast_packet(wire)
        self.assertEqual(len(packet.frames), 1)
        frame = packet.frames[0]
        self.assertEqual(frame.positions, (0, 1))
        self.assertEqual(frame.data, b'\x03')
        self.assertEqual(packet.directory, directory)
        self.assertEqual(self.issues.get(packet.packet_id), wire)

    def test_infeasible_then_feasible_with_more_frames(self):
        directory = self._freeze('shared', (b'\x01', b'\x02', b'\x04'))
        self.assertEqual(self._plan(directory, ((), (0, 1)), ((), (1, 2)), budget=2), 'infeasible')
        wire = self._plan(directory, ((), (0, 1)), ((), (1, 2)), budget=3)
        self.assertIsInstance(wire, bytes)
        self.assertLessEqual(len(decode_broadcast_packet(wire).frames), 3)
        self.assertEqual(self.source.execute('SELECT COUNT(*) FROM issued_packets').fetchone()[0], 1)

    def test_plan_never_uses_unclaimed_bottom(self):
        directory = self._freeze('strict', (b'\x01', b'\x02', b'\x04'))
        wire = self._plan(directory, ((), (1,)), ((), ()), budget=1)
        packet = decode_broadcast_packet(wire)
        self.assertEqual(tuple(frame.positions for frame in packet.frames), ((1,),))
        self.assertEqual(packet.frames[0].data, b'\x02')

    def test_frame_budget_zero_with_empty_requests(self):
        directory = self._freeze('quiet', (b'\x01',))
        wire = self._plan(directory, budget=0)
        packet = decode_broadcast_packet(wire)
        self.assertEqual(packet.frames, ())
        material, receipts, _ = self._desk('A')
        receipt = accept_broadcast(material, receipts, 'A', wire)
        self.assertEqual(receipt.lines, ())
        self.assertEqual(material.get_directory('A', 'quiet', 'R1'), directory)

    def test_plan_validation(self):
        directory = self._freeze('limits', (b'\x01', b'\x02'))
        for kwargs, exception in (
            (dict(budget=-1), ValueError),
            (dict(budget=True), ValueError),
            (dict(a=((0,), (0,))), ValueError),
            (dict(a=((0, 0), ())), ValueError),
            (dict(a=((True,), ())), ValueError),
            (dict(a=((5,), ())), ValueError),
            (dict(a=((0,), (1, 1))), ValueError),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(exception):
                self._plan(directory, **kwargs)
        with self.assertRaises(ValueError):
            plan_broadcast(self.catalog, self.issues, 'limits', 'R1', 'A', (), (), 'A', (), (), 1)
        with self.assertRaises(ValueError):
            plan_broadcast(self.catalog, self.issues, 'limits', 'R1', '', (), (), 'B', (), (), 1)
        with self.assertRaises(LookupError):
            plan_broadcast(self.catalog, self.issues, 'limits', 'R9', 'A', (), (), 'B', (), (), 1)
        self.assertEqual(self.source.execute('SELECT COUNT(*) FROM issued_packets').fetchone()[0], 0)

    def test_seven_positions_rejected(self):
        directory = self._freeze('wide', tuple(bytes([n + 1]) for n in range(7)))
        with self.assertRaises(ValueError):
            self._plan(directory, budget=1)

    def test_v2_wire_rejects_malformed_objects(self):
        directory = self._freeze('wire', (b'\x01', b'\x02'))
        wire = self._plan(directory, ((0,), (1,)), ((1,), (0,)))
        payload = json.loads(wire)
        for mutate in (
            lambda item: item.update(extra=1),
            lambda item: item.update(version=1),
            lambda item: item.update(version=True),
            lambda item: item.pop('frame_budget'),
            lambda item: item['frames'][0].update(data='0A'),
            lambda item: item['frames'][0].update(data='0'),
            lambda item: item['frames'][0].update(positions=[1, 1]),
            lambda item: item['frames'][0].update(positions=[]),
            lambda item: item['frames'][0].update(positions=[2]),
            lambda item: item['frames'].append(dict(item['frames'][0])),
            lambda item: item['stations'][1].update(station='A'),
            lambda item: item['stations'][0].update(claims=[1, 0]),
            lambda item: item['stations'][0].update(claims=[0], wants=[0]),
            lambda item: item['stations'][0].update(claims=[False]),
            lambda item: item.pop('stations'),
            lambda item: item['frames'][0].update(positions=[True]),
        ):
            altered = json.loads(wire)
            mutate(altered)
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                decode_broadcast_packet(json.dumps(altered).encode())
        with self.assertRaises(ValueError):
            decode_broadcast_packet(b'{"version":2,"version":2}')
        with self.assertRaises(ValueError):
            decode_broadcast_packet(b'not-json')
        with self.assertRaises(ValueError):
            accept_broadcast(*(self._desk('A')[:2]), 'A', encode_literal_packet(
                issue(self.catalog, self.issues, 'wire', 'R1', 'A', [0])
            ))

    # --- 点收 -------------------------------------------------------------

    def test_two_stations_share_one_frame(self):
        directory = self._freeze('bench', (b'\x01', b'\x02'))
        self._preload('A', directory, [0])
        self._preload('B', directory, [1])
        wire = self._plan(directory, ((0,), (1,)), ((1,), (0,)))
        for name, target, data in (('A', 1, b'\x02'), ('B', 0, b'\x01')):
            material, receipts, archive = self._desk(name)
            receipt = accept_broadcast(material, receipts, name, wire)
            self.assertEqual(tuple((line.position, line.data) for line in receipt.lines), ((target, data),))
            self.assertEqual(receipt.lines[0].origins, directory.entries[target].origins)
            self.assertEqual(receipt.lines[0].first_receipt_id, receipt.receipt_id)
            report = reconcile(material, archive, name, directory)
            self.assertEqual((report.received, report.missing, report.received_bytes), ((0, 1), (), 2))
            self.assertEqual(accept_broadcast(material, receipts, name, wire), receipt)

    def test_replay_after_reopen_and_spacing(self):
        directory = self._freeze('replay', (b'\x01', b'\x02'))
        self._preload('A', directory, [0])
        wire = self._plan(directory, ((0,), (1,)), ((1,), (0,)))
        material, receipts, _ = self._desk('A')
        first = accept_broadcast(material, receipts, 'A', wire)
        issued = self.source.execute('SELECT COUNT(*) FROM issued_packets').fetchone()[0]
        self._leave()
        material, receipts, archive = self._desk('A')
        replay = json.dumps(json.loads(wire), indent=2).encode()
        self.assertEqual(accept_broadcast(material, receipts, 'A', replay), first)
        self.assertEqual(self.source.execute('SELECT COUNT(*) FROM issued_packets').fetchone()[0], issued)
        self.assertEqual(len(reconcile(material, archive, 'A', directory).received), 2)

    def test_wrong_station_and_changed_content(self):
        directory = self._freeze('address', (b'\x01', b'\x02'))
        self._preload('A', directory, [0])
        wire = self._plan(directory, ((0,), (1,)), ((1,), (0,)))
        material, receipts, _ = self._desk('A')
        with self.assertRaisesRegex(ValueError, 'different station'):
            accept_broadcast(material, receipts, 'C', wire)
        first = accept_broadcast(material, receipts, 'A', wire)
        issued = self.source.execute('SELECT COUNT(*) FROM issued_packets').fetchone()[0]
        payload = json.loads(wire)
        payload['frames'][0]['data'] = '00'
        with self.assertRaises(ValueError):
            accept_broadcast(material, receipts, 'A', json.dumps(payload).encode())
        self.assertEqual(self.source.execute('SELECT COUNT(*) FROM issued_packets').fetchone()[0], issued)
        stored = receipts.find_packet('A', first.packet_id)
        self.assertIsNotNone(stored)

    def test_underdetermined_bottom_and_bad_digest(self):
        directory = self._freeze('depth', (b'\x01', b'\x02'))
        # A claims 0 but the packet declares no claim: it cannot use its stored byte.
        self._preload('A', directory, [0])
        wire = self._plan(directory, ((), (1,)), ((), ()), budget=1)
        payload = json.loads(wire)
        payload['frames'][0]['positions'] = [0, 1]
        payload['frames'][0]['data'] = '03'
        payload['stations'][0]['claims'] = []
        crafted = json.dumps(payload).encode()
        material, receipts, _ = self._desk('A')
        before_blocks = dict(material.read_positions('A', directory))
        before_count = material.connection.execute('SELECT COUNT(*) FROM station_receipts').fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'underdetermined'):
            accept_broadcast(material, receipts, 'A', crafted)
        # A value that satisfies the XOR relation but fails the directory digest.
        valid = self._plan(directory, ((), (1,)), ((), ()), budget=1)
        altered = json.loads(valid)
        altered['frames'][0]['data'] = 'ff'
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            accept_broadcast(material, receipts, 'A', json.dumps(altered).encode())
        self.assertEqual(material.read_positions('A', directory), before_blocks)
        self.assertEqual(
            material.connection.execute('SELECT COUNT(*) FROM station_receipts').fetchone()[0], before_count
        )

    def test_contradictory_frames(self):
        directory = self._freeze('contra', (b'\x01', b'\x02'))
        material, receipts, _ = self._desk('A')
        wire = self._plan(directory, ((), (0, 1)), ((), ()), budget=2)
        payload = json.loads(wire)
        payload['frames'] = [
            {'positions': [0], 'data': '01'},
            {'positions': [0], 'data': '02'},
        ]
        payload['frame_budget'] = 2
        with self.assertRaisesRegex(ValueError, 'contradict'):
            accept_broadcast(material, receipts, 'A', json.dumps(payload).encode())

    def test_equal_byte_distinct_positions(self):
        directory = self._freeze('twins', (b'\x01', b'\x01'))
        self._preload('A', directory, [0])
        wire = self._plan(directory, ((0,), (1,)), ((), ()), budget=1)
        payload = json.loads(wire)
        # 0x01 XOR 0x01 is 0x00: the two equal-byte originals remain distinct positions.
        payload['frames'] = [{'positions': [1, 0], 'data': '00'}]
        material, receipts, _ = self._desk('A')
        receipt = accept_broadcast(material, receipts, 'A', json.dumps(payload).encode())
        self.assertEqual(tuple((line.position, line.data) for line in receipt.lines), ((1, b'\x01'),))

    def test_wrong_revision_bottom_is_unknown(self):
        first = self._freeze('crossrev', (b'\x01', b'\x02'))
        successor = open_successor(self.drafts, build_successor_seed(self.catalog, 'crossrev', 'R1'))
        second = freeze(self.drafts, self.catalog, successor.draft_id, 'R2').directory
        material, receipts, _ = self._desk('A')
        accept(material, receipts, 'A', issue(
            self.catalog, self.issues, 'crossrev', 'R1', 'A', [0, 1]
        ))
        wire = self._plan(second, ((0,), (1,)), ((), ()), budget=1)
        # The packet's bottom claim is for R2, but A only holds R1 material.
        with self.assertRaisesRegex(ValueError, 'underdetermined'):
            accept_broadcast(material, receipts, 'A', wire)
        self.assertNotEqual(first, second)

    def test_unused_corrupt_bottom_does_not_block_targets(self):
        directory = self._freeze('unused', (b'\x01', b'\x02', b'\x04'))
        self._preload('A', directory, [0, 1, 2])
        material, receipts, _ = self._desk('A')
        material.connection.execute(
            "UPDATE station_blocks SET data = x'ff' WHERE station = 'A' AND position = 2"
        )
        material.connection.commit()
        # Only position 0 is needed; the corrupt but unrequested position 2 is harmless.
        wire = self._plan(directory, ((0,), (1,)), ((), ()), budget=1)
        receipt = accept_broadcast(material, receipts, 'A', wire)
        self.assertEqual(tuple((line.position, line.data) for line in receipt.lines), ((1, b'\x02'),))

    def test_replay_after_bottom_changes(self):
        directory = self._freeze('drift', (b'\x01', b'\x02'))
        self._preload('A', directory, [0])
        wire = self._plan(directory, ((0,), (1,)), ((1,), (0,)))
        material, receipts, _ = self._desk('A')
        first = accept_broadcast(material, receipts, 'A', wire)
        material.connection.execute(
            "UPDATE station_blocks SET data = x'ff' WHERE station = 'A' AND position = 0"
        )
        material.connection.commit()
        self.assertEqual(accept_broadcast(material, receipts, 'A', wire), first)

    # --- 台架样例 ---------------------------------------------------------

    def test_different_bottom_eligibility(self):
        directory = self._freeze('sample', (b'\x01', b'\x02', b'\x04'))
        self._preload('A', directory, [0, 2])
        self._preload('B', directory, [1])
        first = json.loads(self._plan(directory, ((0, 2), (1,)), ((1,), (2, 0)), budget=2))
        second = json.loads(json.dumps(first))
        first['frames'] = [{'positions': [0, 1], 'data': '03'}, {'positions': [2], 'data': '04'}]
        second['frames'] = [{'positions': [0, 1], 'data': '03'}, {'positions': [0, 2], 'data': '05'}]

        for name, order in (('A', (1,)), ('B', (2, 0))):
            material, receipts, _ = self._desk(name)
            receipt = accept_broadcast(material, receipts, name, json.dumps(first).encode())
            self.assertEqual(tuple(line.position for line in receipt.lines), order)

        # Corrupt only A's cached position 0 and try the same first packet on fresh desks.
        self._leave()
        for path in self.desk_paths.values():
            path.unlink(missing_ok=True)
        self._preload('A', directory, [0, 2])
        self._preload('B', directory, [1])
        material_a, _, _ = self._desk('A')
        material_a.connection.execute(
            "UPDATE station_blocks SET data = x'ff' WHERE station = 'A' AND position = 0"
        )
        material_a.connection.commit()

        material, receipts, _ = self._desk('A')
        with self.assertRaisesRegex(ValueError, 'underdetermined'):
            accept_broadcast(material, receipts, 'A', json.dumps(first).encode())
        receipt_a = accept_broadcast(material, receipts, 'A', json.dumps(second).encode())
        self.assertEqual(tuple((line.position, line.data) for line in receipt_a.lines), ((1, b'\x02'),))

        material_b, receipts_b, _ = self._desk('B')
        receipt_b = accept_broadcast(material_b, receipts_b, 'B', json.dumps(second).encode())
        self.assertEqual(tuple((line.position, line.data) for line in receipt_b.lines),
                         ((2, b'\x04'), (0, b'\x01')))

    def test_only_targets_must_be_determined(self):
        directory = self._freeze('target', (b'\x01', b'\x02', b'\x04'), width=1)
        literal = self._plan(directory, ((), (1,)), ((), ()), budget=1)
        self._leave()
        wire = decode_broadcast_packet(literal)
        self.assertEqual(tuple(frame.positions for frame in wire.frames), ((1,),))
        payload = json.loads(literal)
        payload['frames'] = [{'positions': [0, 1], 'data': '03'}]
        material, receipts, _ = self._desk('A')
        with self.assertRaisesRegex(ValueError, 'underdetermined'):
            accept_broadcast(material, receipts, 'A', json.dumps(payload).encode())
        self.assertIsNone(material.get_directory('A', 'target', 'R1'))

    # --- 失败边界 ---------------------------------------------------------

    def test_failed_write_leaves_no_partial_state(self):
        directory = self._freeze('atomic', (b'\x01', b'\x02'))
        self._preload('A', directory, [0])
        wire = self._plan(directory, ((0,), (1,)), ((1,), (0,)))
        connection = self.desks['A']
        connection.execute('''
            CREATE TRIGGER refuse_broadcast BEFORE INSERT ON station_blocks
            WHEN NEW.position = 1
            BEGIN SELECT RAISE(ABORT, 'desk ledger refuses broadcast'); END
        ''')
        material, receipts, _ = self._desk('A')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'refuses broadcast'):
            accept_broadcast(material, receipts, 'A', wire)
        self.assertEqual(tuple(material.read_positions('A', directory)), (0,))
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM station_receipts').fetchone()[0], 1)
        self.assertIsNone(receipts.find_packet('A', decode_broadcast_packet(wire).packet_id))
        connection.execute('DROP TRIGGER refuse_broadcast')
        receipt = accept_broadcast(material, receipts, 'A', wire)
        self.assertEqual(tuple(line.position for line in receipt.lines), (1,))

    def test_first_failure_rolls_back_directory(self):
        directory = self._freeze('fresh', (b'\x01', b'\x02'))
        wire = self._plan(directory, ((), (0,)), ((), (1,)), budget=2)
        connection = sqlite3.connect(self.desk_paths['A'])
        self.addCleanup(connection.close)
        material, receipts = StationMaterial(connection), ReceiptStore(connection)
        connection.execute('''
            CREATE TRIGGER refuse_snapshot BEFORE UPDATE OF snapshot ON station_receipts
            BEGIN SELECT RAISE(ABORT, 'desk ledger refuses snapshot'); END
        ''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'refuses snapshot'):
            accept_broadcast(material, receipts, 'A', wire)
        self.assertIsNone(material.get_directory('A', 'fresh', 'R1'))
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM station_receipts').fetchone()[0], 0)
        self.assertEqual(material.read_positions('A', directory), {})

    def test_bad_directory_conflicts(self):
        directory = self._freeze('conflict', (b'\x01', b'\x02'))
        wire = self._plan(directory, ((0,), (1,)), ((1,), (0,)))
        self._preload('A', directory, [0])
        material, receipts, _ = self._desk('A')
        issued = self.source.execute('SELECT COUNT(*) FROM issued_packets').fetchone()[0]

        alternate = build_directory(directory.layout, 'R1', (
            PlacedBlock(0, b'\x01', (BlockOrigin('device', 'device-0'),)),
            PlacedBlock(1, b'\x02', (BlockOrigin('device', 'different-evidence'),)),
        ))
        payload = json.loads(wire)
        payload['directory'] = json.loads(json.dumps(directory_to_data(alternate)))
        with self.assertRaisesRegex(ValueError, 'conflicts with station directory'):
            accept_broadcast(material, receipts, 'A', json.dumps(payload).encode())

        payload = json.loads(wire)
        payload['directory']['revision'] = 'R2'
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            accept_broadcast(material, receipts, 'A', json.dumps(payload).encode())
        self.assertEqual(self.source.execute('SELECT COUNT(*) FROM issued_packets').fetchone()[0], issued)
        self.assertEqual(material.get_directory('A', 'conflict', 'R1'), directory)

    def test_literal_history_and_regression_untouched(self):
        directory = self._freeze('legacy', (b'\x01',))
        literal = issue(self.catalog, self.issues, 'legacy', 'R1', 'A', [0])
        self.assertIsInstance(self.issues.get(literal.packet_id), type(literal))
        wire = self._plan(directory, ((0,), ()), ((), ()), budget=0)
        self.assertTrue(decode_broadcast_packet(wire).packet_id.startswith('I-'))
        self.assertNotEqual(decode_broadcast_packet(wire).packet_id, literal.packet_id)
        self.assertIsInstance(self.issues.get(decode_broadcast_packet(wire).packet_id), bytes)
        self.assertEqual(decode_literal_packet(encode_literal_packet(literal)), literal)


if __name__ == '__main__':
    unittest.main()
