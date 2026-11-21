from collections.abc import Sequence

from edition.directory import RevisionDirectory
from intake.layout import validate_position


class _AllPositions:
    pass


ALL_POSITIONS = _AllPositions()
PositionSelection = Sequence[int] | _AllPositions


def resolve_selection(directory: RevisionDirectory, selection: PositionSelection) -> tuple[int, ...]:
    if selection is ALL_POSITIONS:
        return tuple(entry.position for entry in directory.entries)
    if not isinstance(selection, Sequence) or isinstance(selection, (str, bytes, bytearray, memoryview)):
        raise ValueError('expected an ordered original-position selection')
    layout = directory.layout
    ordered = []
    seen = set()
    for position in selection:
        validate_position(layout, position)
        if position in seen:
            raise ValueError(f'duplicate selected position: {position}')
        seen.add(position)
        ordered.append(position)
    return tuple(ordered)
