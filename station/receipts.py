import json
import sqlite3
from dataclasses import asdict, dataclass

from edition.directory import FrozenOrigin, RevisionDirectory, directory_from_data, directory_to_data, origins_from_data


@dataclass(frozen=True)
class ReceiptLine:
    position: int
    data: bytes
    origins: tuple[FrozenOrigin, ...]
    first_receipt_id: str


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    station: str
    packet_id: str
    directory: RevisionDirectory
    lines: tuple[ReceiptLine, ...]


def _receipt_snapshot(receipt: Receipt) -> str:
    return json.dumps({
        'receipt_id': receipt.receipt_id,
        'station': receipt.station,
        'packet_id': receipt.packet_id,
        'directory': directory_to_data(receipt.directory),
        'lines': [
            {
                'position': line.position,
                'data': line.data.hex(),
                'origins': [asdict(origin) for origin in line.origins],
                'first_receipt_id': line.first_receipt_id,
            }
            for line in receipt.lines
        ],
    }, separators=(',', ':'))


def _read_snapshot(snapshot: str) -> Receipt:
    data = json.loads(snapshot)
    lines = tuple(ReceiptLine(
        item['position'], bytes.fromhex(item['data']), origins_from_data(item['origins']), item['first_receipt_id']
    ) for item in data['lines'])
    return Receipt(data['receipt_id'], data['station'], data['packet_id'], directory_from_data(data['directory']), lines)


class ReceiptStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute('''
            CREATE TABLE IF NOT EXISTS station_receipts (
                serial INTEGER PRIMARY KEY,
                station TEXT NOT NULL,
                packet_id TEXT NOT NULL,
                canonical BLOB NOT NULL,
                receipt_id TEXT UNIQUE,
                snapshot TEXT,
                UNIQUE (station, packet_id)
            )
        ''')

    def find_packet(self, station: str, packet_id: str) -> tuple[bytes, Receipt] | None:
        row = self.connection.execute('''
            SELECT canonical, snapshot FROM station_receipts
            WHERE station = ? AND packet_id = ? AND snapshot IS NOT NULL
        ''', (station, packet_id)).fetchone()
        return (row[0], _read_snapshot(row[1])) if row else None

    def reserve_receipt(self, station: str, packet_id: str, canonical: bytes) -> str:
        cursor = self.connection.execute('''
            INSERT INTO station_receipts (station, packet_id, canonical) VALUES (?, ?, ?)
        ''', (station, packet_id, canonical))
        receipt_id = f'R-{cursor.lastrowid}'
        self.connection.execute(
            'UPDATE station_receipts SET receipt_id = ? WHERE serial = ?', (receipt_id, cursor.lastrowid)
        )
        return receipt_id

    def save(self, receipt: Receipt) -> None:
        cursor = self.connection.execute('''
            UPDATE station_receipts SET snapshot = ?
            WHERE station = ? AND packet_id = ? AND receipt_id = ? AND snapshot IS NULL
        ''', (_receipt_snapshot(receipt), receipt.station, receipt.packet_id, receipt.receipt_id))
        if cursor.rowcount != 1:
            raise ValueError('receipt does not match an open reservation')

    def get(self, station: str, receipt_id: str) -> Receipt | None:
        row = self.connection.execute('''
            SELECT snapshot FROM station_receipts
            WHERE station = ? AND receipt_id = ? AND snapshot IS NOT NULL
        ''', (station, receipt_id)).fetchone()
        return _read_snapshot(row[0]) if row else None
