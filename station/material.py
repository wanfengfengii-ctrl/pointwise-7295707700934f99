import json
import sqlite3
from dataclasses import dataclass

from edition.directory import FrozenOrigin, RevisionDirectory, directory_from_data, directory_to_data


@dataclass(frozen=True)
class StationBlock:
    position: int
    data: bytes
    origins: tuple[FrozenOrigin, ...]
    first_receipt_id: str


class StationMaterial:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute('''
            CREATE TABLE IF NOT EXISTS station_directories (
                station TEXT NOT NULL,
                volume TEXT NOT NULL,
                revision TEXT NOT NULL,
                directory TEXT NOT NULL,
                PRIMARY KEY (station, volume, revision)
            )
        ''')
        connection.execute('''
            CREATE TABLE IF NOT EXISTS station_blocks (
                station TEXT NOT NULL,
                volume TEXT NOT NULL,
                revision TEXT NOT NULL,
                position INTEGER NOT NULL,
                data BLOB NOT NULL,
                first_receipt_id TEXT NOT NULL,
                PRIMARY KEY (station, volume, revision, position),
                FOREIGN KEY (station, volume, revision) REFERENCES station_directories(station, volume, revision)
            )
        ''')

    def get_directory(self, station: str, volume: str, revision: str) -> RevisionDirectory | None:
        row = self.connection.execute('''
            SELECT directory FROM station_directories WHERE station = ? AND volume = ? AND revision = ?
        ''', (station, volume, revision)).fetchone()
        return directory_from_data(json.loads(row[0])) if row else None

    def record_directory(self, station: str, directory: RevisionDirectory) -> None:
        self.connection.execute('''
            INSERT INTO station_directories (station, volume, revision, directory) VALUES (?, ?, ?, ?)
        ''', (station, directory.volume, directory.revision, json.dumps(directory_to_data(directory))))

    def read_positions(self, station: str, directory: RevisionDirectory) -> dict[int, StationBlock]:
        rows = self.connection.execute('''
            SELECT position, data, first_receipt_id FROM station_blocks
            WHERE station = ? AND volume = ? AND revision = ? ORDER BY position
        ''', (station, directory.volume, directory.revision))
        return {
            position: StationBlock(position, data, directory.entries[position].origins, first_receipt)
            for position, data, first_receipt in rows
        }

    def insert_first(self, station: str, directory: RevisionDirectory, block: StationBlock) -> None:
        self.connection.execute('''
            INSERT INTO station_blocks (station, volume, revision, position, data, first_receipt_id)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (station, directory.volume, directory.revision, block.position, block.data, block.first_receipt_id))
