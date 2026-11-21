from intake.batches import DraftSeed, DraftStore
from intake.layout import validate_label
from intake.placement import BlockOrigin, PlacedBlock

from .catalog import FrozenRevision, RevisionCatalog
from .directory import build_directory


def freeze(drafts: DraftStore, catalog: RevisionCatalog, draft_id: int, revision: str) -> FrozenRevision:
    validate_label(revision, 'revision')
    if drafts.connection is not catalog.connection:
        raise ValueError('freezing requires one source connection')
    with drafts.write_transaction():
        draft = drafts.get(draft_id)
        if draft is None:
            raise LookupError('unknown draft')
        if len(draft.blocks) != draft.layout.slot_count:
            raise ValueError('draft is missing positions')
        directory = build_directory(draft.layout, revision, draft.blocks)
        bodies = tuple(block.data for block in sorted(draft.blocks, key=lambda block: block.position))
        frozen = FrozenRevision(directory, bodies)
        catalog.insert(frozen)
        return frozen


def build_successor_seed(catalog: RevisionCatalog, volume: str, parent_revision: str) -> DraftSeed:
    validate_label(volume, 'volume')
    validate_label(parent_revision, 'parent revision')
    frozen = catalog.get(volume, parent_revision)
    if frozen is None:
        raise LookupError('unknown parent revision')
    blocks = []
    for entry, data in zip(frozen.directory.entries, frozen.bodies, strict=True):
        inherited = tuple(BlockOrigin(origin.kind, origin.reference) for origin in entry.origins)
        blocks.append(PlacedBlock(
            entry.position,
            data,
            inherited + (BlockOrigin('inheritance', parent_revision),),
        ))
    return DraftSeed(frozen.directory.layout, parent_revision, tuple(blocks))
