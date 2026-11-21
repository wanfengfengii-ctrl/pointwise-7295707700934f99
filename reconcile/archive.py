import json
import sqlite3
from dataclasses import asdict, replace

from edition.directory import directory_from_data, origins_from_data

from .accounting import Statement, StatementLine


def _load_statement(snapshot: str) -> Statement:
    data = json.loads(snapshot)
    return Statement(
        data['report_id'],
        data['station'],
        directory_from_data(data['directory']),
        tuple(data['received']),
        tuple(data['missing']),
        data['received_bytes'],
        tuple(StatementLine(
            line['position'], line['byte_count'], line['first_receipt_id'], origins_from_data(line['origins'])
        ) for line in data['lines']),
    )


class StatementArchive:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute('''
            CREATE TABLE IF NOT EXISTS station_statements (
                serial INTEGER PRIMARY KEY,
                station TEXT NOT NULL,
                report_id TEXT UNIQUE,
                snapshot TEXT
            )
        ''')

    def append(self, statement: Statement) -> Statement:
        cursor = self.connection.execute('INSERT INTO station_statements (station) VALUES (?)', (statement.station,))
        saved = replace(statement, report_id=f'S-{cursor.lastrowid}')
        self.connection.execute(
            'UPDATE station_statements SET report_id = ?, snapshot = ? WHERE serial = ?',
            (saved.report_id, json.dumps(asdict(saved), separators=(',', ':')), cursor.lastrowid),
        )
        return saved

    def get(self, station: str, report_id: str) -> Statement | None:
        row = self.connection.execute('''
            SELECT snapshot FROM station_statements
            WHERE station = ? AND report_id = ? AND snapshot IS NOT NULL
        ''', (station, report_id)).fetchone()
        return _load_statement(row[0]) if row else None
