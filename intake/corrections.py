from dataclasses import dataclass, replace

from .batches import Draft, DraftStore
from .layout import validate_block_width, validate_label, validate_position
from .placement import BlockOrigin, PlacedBlock


@dataclass(frozen=True)
class Correction:
    position: int
    data: bytes
    reference: str


def correct(store: DraftStore, draft_id: int, correction: Correction) -> Draft:
    if type(draft_id) is not int or draft_id <= 0:
        raise ValueError('corrections require an editable draft ID')
    if not isinstance(correction, Correction):
        raise ValueError('expected a correction')
    validate_label(correction.reference, 'correction reference')
    with store.write_transaction():
        draft = store.get(draft_id)
        if draft is None:
            raise LookupError('unknown draft')
        validate_position(draft.layout, correction.position)
        validate_block_width(draft.layout, correction.data)
        blocks = {block.position: block for block in draft.blocks}
        original = blocks.get(correction.position)
        if original is None:
            raise ValueError('cannot correct a position without a current block')
        blocks[correction.position] = PlacedBlock(
            correction.position,
            correction.data,
            original.origins + (BlockOrigin('correction', correction.reference),),
        )
        return store.save(replace(draft, blocks=tuple(blocks.values())))
