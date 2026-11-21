import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace

from .layout import DraftLayout, validate_block_width, validate_label, validate_layout
from .placement import BlockOrigin, PlacedBlock, fill_open_positions
from .segments import DeviceSegment, stage_segments


@dataclass(frozen=True)
class Draft:
    layout: DraftLayout
    draft_id: int | None = None
    parent_revision: str | None = None
    blocks: tuple[PlacedBlock, ...] = ()
    segments: tuple[DeviceSegment, ...] = ()


@dataclass(frozen=True)
class DraftSeed:
    layout: DraftLayout
    parent_revision: str
    blocks: tuple[PlacedBlock, ...]


class DraftStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute('''
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY,
                volume TEXT NOT NULL,
                parent_revision TEXT NOT NULL,
                state TEXT NOT NULL,
                UNIQUE (volume, parent_revision)
            )
        ''')
        connection.execute('''
            CREATE TABLE IF NOT EXISTS current_drafts (
                volume TEXT PRIMARY KEY,
                draft_id INTEGER NOT NULL REFERENCES drafts(id)
            )
        ''')

    @contextmanager
    def write_transaction(self) -> Iterator[None]:
        self.connection.execute('BEGIN IMMEDIATE')
        with self.connection:
            yield

    def get(self, draft_id: int) -> Draft | None:
        if type(draft_id) is not int or not 0 < draft_id < 2**63:
            return None
        row = self.connection.execute(
            'SELECT volume, parent_revision, state FROM drafts WHERE id = ?', (draft_id,)
        ).fetchone()
        if row is None:
            return None
        volume, parent, serialized = row
        state = json.loads(serialized)
        return Draft(
            layout=DraftLayout(volume, state['slot_count'], state['width']),
            draft_id=draft_id,
            parent_revision=parent or None,
            blocks=tuple(
                PlacedBlock(position, bytes.fromhex(data), tuple(BlockOrigin(*origin) for origin in origins))
                for position, data, origins in state['blocks']
            ),
            segments=tuple(
                DeviceSegment(reference, position, bytes.fromhex(data))
                for reference, position, data in state['segments']
            ),
        )

    def save(self, draft: Draft) -> Draft:
        state = json.dumps({
            'slot_count': draft.layout.slot_count,
            'width': draft.layout.width,
            'blocks': [
                (block.position, block.data.hex(), [(origin.kind, origin.reference) for origin in block.origins])
                for block in draft.blocks
            ],
            'segments': [(segment.reference, segment.position, segment.data.hex()) for segment in draft.segments],
        }, separators=(',', ':'))
        if draft.draft_id is None:
            cursor = self.connection.execute(
                'INSERT INTO drafts (volume, parent_revision, state) VALUES (?, ?, ?)',
                (draft.layout.volume, draft.parent_revision or '', state),
            )
            return replace(draft, draft_id=cursor.lastrowid)
        cursor = self.connection.execute(
            'UPDATE drafts SET state = ? WHERE id = ? AND volume = ? AND parent_revision = ?',
            (state, draft.draft_id, draft.layout.volume, draft.parent_revision or ''),
        )
        if cursor.rowcount != 1:
            raise LookupError('unknown draft')
        return draft

    def _current(self, volume: str) -> Draft | None:
        row = self.connection.execute(
            'SELECT draft_id FROM current_drafts WHERE volume = ?', (volume,)
        ).fetchone()
        return self.get(row[0]) if row else None

    def _from_parent(self, volume: str, parent: str) -> Draft | None:
        row = self.connection.execute(
            'SELECT id FROM drafts WHERE volume = ? AND parent_revision = ?', (volume, parent)
        ).fetchone()
        return self.get(row[0]) if row else None

    def _install(self, draft: Draft) -> None:
        self.connection.execute('''
            INSERT INTO current_drafts (volume, draft_id) VALUES (?, ?)
            ON CONFLICT (volume) DO UPDATE SET draft_id = excluded.draft_id
        ''', (draft.layout.volume, draft.draft_id))


def open_and_load(
    store: DraftStore,
    layout: DraftLayout,
    segments: Iterable[DeviceSegment] = (),
) -> Draft:
    validate_layout(layout)
    with store.write_transaction():
        draft = store._current(layout.volume)
        if draft is not None and draft.layout != layout:
            raise ValueError('layout conflicts with current draft')
        new_draft = draft is None
        if new_draft:
            draft = Draft(layout)
        added = stage_segments(layout, draft.segments, segments)
        if new_draft or added:
            draft = store.save(replace(
                draft,
                segments=draft.segments + added,
                blocks=fill_open_positions(draft.blocks, added),
            ))
        if new_draft:
            store._install(draft)
        return draft


def open_successor(store: DraftStore, seed: DraftSeed) -> Draft:
    if not isinstance(seed, DraftSeed):
        raise ValueError('expected a successor seed from a frozen revision')
    validate_layout(seed.layout)
    validate_label(seed.parent_revision, 'parent revision')
    if not isinstance(seed.blocks, tuple) or len(seed.blocks) != seed.layout.slot_count:
        raise ValueError('successor seed must cover the layout')
    for position, block in enumerate(seed.blocks):
        if not isinstance(block, PlacedBlock) or type(block.position) is not int or block.position != position:
            raise ValueError('successor seed must be in original position order')
        validate_block_width(seed.layout, block.data)
        if not block.origins or any(not isinstance(origin, BlockOrigin) for origin in block.origins):
            raise ValueError('successor seed requires frozen origins')
    with store.write_transaction():
        current = store._current(seed.layout.volume)
        if current is not None and current.layout != seed.layout:
            raise ValueError('parent layout conflicts with current draft')
        draft = store._from_parent(seed.layout.volume, seed.parent_revision)
        if draft is None:
            draft = store.save(Draft(seed.layout, parent_revision=seed.parent_revision, blocks=seed.blocks))
        store._install(draft)
        return draft
