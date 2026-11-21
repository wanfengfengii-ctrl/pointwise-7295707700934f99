import sqlite3
from collections.abc import Callable

from .manifest import LiteralManifest
from .wire import decode_literal_packet, encode_literal_packet


class IssueHistory:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute('''
            CREATE TABLE IF NOT EXISTS issued_packets (
                serial INTEGER PRIMARY KEY,
                packet_id TEXT UNIQUE,
                wire BLOB
            )
        ''')

    def append(self, make_packet: Callable[[str], LiteralManifest]) -> LiteralManifest:
        self.connection.execute('BEGIN IMMEDIATE')
        with self.connection:
            cursor = self.connection.execute('INSERT INTO issued_packets DEFAULT VALUES')
            packet_id = f'I-{cursor.lastrowid}'
            packet = make_packet(packet_id)
            if packet.packet_id != packet_id:
                raise ValueError('packet must use its assigned issue number')
            self.connection.execute(
                'UPDATE issued_packets SET packet_id = ?, wire = ? WHERE serial = ?',
                (packet_id, encode_literal_packet(packet), cursor.lastrowid),
            )
            return packet

    def get(self, packet_id: str) -> LiteralManifest | None:
        if not isinstance(packet_id, str):
            return None
        row = self.connection.execute(
            'SELECT wire FROM issued_packets WHERE packet_id = ?', (packet_id,)
        ).fetchone()
        return decode_literal_packet(row[0]) if row else None
