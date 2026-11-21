from dataclasses import dataclass

from .layout import DraftLayout
from .segments import DeviceSegment


@dataclass(frozen=True)
class BlockOrigin:
    kind: str
    reference: str


@dataclass(frozen=True)
class PlacedBlock:
    position: int
    data: bytes
    origins: tuple[BlockOrigin, ...]


def fill_open_positions(
    current: tuple[PlacedBlock, ...],
    added: tuple[DeviceSegment, ...],
) -> tuple[PlacedBlock, ...]:
    by_position = {block.position: block for block in current}
    for segment in added:
        if segment.position not in by_position:
            by_position[segment.position] = PlacedBlock(
                segment.position,
                segment.data,
                (BlockOrigin('device', segment.reference),),
            )
    return tuple(by_position[position] for position in sorted(by_position))


def missing_positions(layout: DraftLayout, blocks: tuple[PlacedBlock, ...]) -> tuple[int, ...]:
    present = {block.position for block in blocks}
    return tuple(position for position in range(layout.slot_count) if position not in present)
