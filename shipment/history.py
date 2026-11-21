import json
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

    def _append(self, make_wire: Callable[[str], bytes]) -> tuple[str, bytes]:
        self.connection.execute('BEGIN IMMEDIATE')
        with self.connection:
            cursor = self.connection.execute('INSERT INTO issued_packets DEFAULT VALUES')
            packet_id = f'I-{cursor.lastrowid}'
            wire = make_wire(packet_id)
            self.connection.execute(
                'UPDATE issued_packets SET packet_id = ?, wire = ? WHERE serial = ?',
                (packet_id, wire, cursor.lastrowid),
            )
            return packet_id, wire

    def append(self, make_packet: Callable[[str], LiteralManifest]) -> LiteralManifest:
        holder: dict[str, LiteralManifest] = {}

        def make_wire(packet_id: str) -> bytes:
            packet = make_packet(packet_id)
            if packet.packet_id != packet_id:
                raise ValueError('packet must use its assigned issue number')
            holder['packet'] = packet
            return encode_literal_packet(packet)

        self._append(make_wire)
        return holder['packet']

    def append_wire(self, make_wire: Callable[[str], bytes]) -> bytes:
        """Store a non-literal (V2) packet in the shared I- number space."""
        _, wire = self._append(make_wire)
        return wire

    def get(self, packet_id: str) -> LiteralManifest | bytes | None:
        if not isinstance(packet_id, str):
            return None
        row = self.connection.execute(
            'SELECT wire FROM issued_packets WHERE packet_id = ?', (packet_id,)
        ).fetchone()
        if not row:
            return None
        wire = row[0]
        try:
            payload = json.loads(bytes(wire).decode('utf-8'))
        except (UnicodeError, ValueError):
            return decode_literal_packet(wire)
        if isinstance(payload, dict) and payload.get('version') == 2:
            return bytes(wire)
        return decode_literal_packet(wire)
