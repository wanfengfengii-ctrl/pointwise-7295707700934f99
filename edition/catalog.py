import json
import sqlite3
from dataclasses import dataclass

from .directory import RevisionDirectory, directory_from_data, directory_to_data


@dataclass(frozen=True)
class FrozenRevision:
    directory: RevisionDirectory
    bodies: tuple[bytes, ...]


class RevisionCatalog:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute('''
            CREATE TABLE IF NOT EXISTS frozen_revisions (
                volume TEXT NOT NULL,
                revision TEXT NOT NULL,
                directory TEXT NOT NULL,
                bodies BLOB NOT NULL,
                PRIMARY KEY (volume, revision)
            )
        ''')

    def insert(self, frozen: FrozenRevision) -> None:
        directory = frozen.directory
        cursor = self.connection.execute('''
            INSERT INTO frozen_revisions (volume, revision, directory, bodies) VALUES (?, ?, ?, ?)
            ON CONFLICT (volume, revision) DO NOTHING
        ''', (
            directory.volume,
            directory.revision,
            json.dumps(directory_to_data(directory), separators=(',', ':')),
            b''.join(frozen.bodies),
        ))
        if cursor.rowcount != 1:
            raise ValueError('revision already frozen for this volume')

    def get(self, volume: str, revision: str) -> FrozenRevision | None:
        row = self.connection.execute('''
            SELECT directory, bodies FROM frozen_revisions WHERE volume = ? AND revision = ?
        ''', (volume, revision)).fetchone()
        if row is None:
            return None
        serialized, packed = row
        directory = directory_from_data(json.loads(serialized))
        if len(packed) != directory.total_bytes:
            raise ValueError('stored revision body length disagrees with its directory')
        width = directory.width
        bodies = tuple(packed[offset:offset + width] for offset in range(0, len(packed), width))
        return FrozenRevision(directory, bodies)
