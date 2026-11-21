import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from edition import RevisionCatalog, build_successor_seed, freeze
from intake import Correction, DeviceSegment, DraftLayout, DraftStore, correct, missing_positions, open_and_load, open_successor
from reconcile import StatementArchive, read_history, reconcile
from shipment import IssueHistory, encode_literal_packet, issue
from station import ReceiptStore, StationMaterial, accept
from tests.broadcast import BENCH_FRAME_BUDGET, broadcast


def verify(label, actual, expected):
    if actual != expected:
        raise RuntimeError(f'{label}: expected {expected!r}, received {actual!r}')


def metrics(statement):
    return {'received': list(statement.received), 'missing': list(statement.missing), 'bytes': statement.received_bytes}


def run_day(root):
    source_path = root / 'hanchuan.sqlite'
    station_path = root / 'desk-a.sqlite'
    layout = DraftLayout('pressure-day', 2, 1)
    with closing(sqlite3.connect(source_path)) as source:
        drafts, catalog, issues = DraftStore(source), RevisionCatalog(source), IssueHistory(source)
        late_slot = DeviceSegment('pressure-1', 1, bytes.fromhex('01'))
        draft = open_and_load(drafts, layout, (late_slot,))
        verify('intake gap', missing_positions(layout, draft.blocks), (0,))
        try:
            freeze(drafts, catalog, draft.draft_id, 'R1')
        except ValueError:
            verify('rejected freeze', catalog.get(layout.volume, 'R1'), None)
        else:
            raise RuntimeError('incomplete draft was frozen')
        ready = open_and_load(drafts, layout, (DeviceSegment('pressure-0', 0, bytes.fromhex('01')), late_slot))
        verify('distinct device originals', len(ready.segments), 2)
        base = freeze(drafts, catalog, ready.draft_id, 'R1')
        verify('equal bytes counted at both positions', base.directory.total_bytes, 2)
        successor = open_successor(drafts, build_successor_seed(catalog, layout.volume, 'R1'))
        amended = correct(drafts, successor.draft_id, Correction(1, bytes.fromhex('02'), 'review-1'))
        verify('reopened successor', open_and_load(drafts, layout), amended)
        frozen = freeze(drafts, catalog, amended.draft_id, 'R2')
        verify('frozen bodies', frozen.bodies, (b'\x01', b'\x02'))
        verify('parent bodies remain available', catalog.get(layout.volume, 'R1').bodies, (b'\x01', b'\x01'))
        first = issue(catalog, issues, layout.volume, 'R2', 'A', [1])
        full_for_b = encode_literal_packet(issue(catalog, issues, layout.volume, 'R2', 'B'))
        delivered = broadcast(encode_literal_packet(first), len(first.blocks))
        verify('literal frame budget', delivered.frame_count, BENCH_FRAME_BUDGET)
    with closing(sqlite3.connect(root / 'desk-b.sqlite')) as station_b:
        receipt_b = accept(StationMaterial(station_b), ReceiptStore(station_b), 'B', full_for_b)
        verify('B full volume', tuple(line.data.hex() for line in receipt_b.lines), ('01', '02'))
    with closing(sqlite3.connect(station_path)) as station_a:
        material, receipts, archive = StationMaterial(station_a), ReceiptStore(station_a), StatementArchive(station_a)
        initial = accept(material, receipts, 'A', delivered.copies[0])
        partial = reconcile(material, archive, 'A', initial.directory)
        partial_expected = {'received': [1], 'missing': [0], 'bytes': 1}
        verify('partial report', metrics(partial), partial_expected)
        with closing(sqlite3.connect(source_path)) as source:
            top_up = issue(RevisionCatalog(source), IssueHistory(source), layout.volume, 'R2', 'A', [1, *partial.missing])
        wire = encode_literal_packet(top_up)
        later = accept(material, receipts, 'A', wire)
        verify('supplement order and first receipt', tuple((line.position, line.first_receipt_id) for line in later.lines),
               ((1, initial.receipt_id), (0, later.receipt_id)))
        verify('supplement replay', accept(material, receipts, 'A', wire), later)
        complete = reconcile(material, archive, 'A', later.directory)
        complete_expected = {'received': [0, 1], 'missing': [], 'bytes': 2}
        verify('complete report', metrics(complete), complete_expected)
    source_path.rename(root / 'hanchuan.offline')
    with closing(sqlite3.connect(station_path)) as station_a:
        receipts, archive = ReceiptStore(station_a), StatementArchive(station_a)
        saved_partial = read_history(receipts, archive, 'A', partial.report_id)
        saved_complete = read_history(receipts, archive, 'A', complete.report_id)
        verify('offline partial', saved_partial, partial)
        verify('offline complete', saved_complete, complete)
        for receipt in (initial, later):
            verify('offline receipt', read_history(receipts, archive, 'A', receipt.receipt_id), receipt)
    print('POINTWISE_SCENARIO ' + json.dumps({
        'case_id': 'v8',
        'input': {'revision': 'R2', 'width': 1, 'original_hex': ['01', '02'], 'first_order': [1], 'supplement_order': [1, 0]},
        'expected': partial_expected,
        'actual': metrics(saved_partial),
        'downstream_expected': complete_expected,
        'downstream_actual': metrics(saved_complete),
        'code_paths': ['edition/freeze.py', 'shipment/issue.py', 'station/commit.py', 'reconcile/coverage.py', 'reconcile/accounting.py', 'reconcile/history.py'],
        'oracle': 'Position 1 alone covers one byte and leaves position 0 due. The supplement adds exactly one new one-byte position.',
    }, sort_keys=True))
    print(f'A：欠件报告 {partial.report_id}，完卷报告 {complete.report_id}，实收首执 {initial.receipt_id}，补件回执 {later.receipt_id}')


if __name__ == '__main__':
    with TemporaryDirectory(prefix='hanchuan-exchange-') as work:
        run_day(Path(work))
