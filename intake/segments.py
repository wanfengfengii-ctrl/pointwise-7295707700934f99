from collections.abc import Iterable
from dataclasses import dataclass

from .layout import DraftLayout, validate_block_width, validate_label, validate_position


@dataclass(frozen=True)
class DeviceSegment:
    reference: str
    position: int
    data: bytes


def stage_segments(
    layout: DraftLayout,
    existing: tuple[DeviceSegment, ...],
    incoming: Iterable[DeviceSegment],
) -> tuple[DeviceSegment, ...]:
    """Return the new evidence in delivery order after checking the whole batch."""
    known = {segment.reference: segment for segment in existing}
    added = []
    try:
        entries = iter(incoming)
    except TypeError as error:
        raise ValueError('expected an iterable of device segments') from error
    for segment in entries:
        if not isinstance(segment, DeviceSegment):
            raise ValueError('expected a device segment')
        validate_label(segment.reference, 'segment reference')
        validate_position(layout, segment.position)
        validate_block_width(layout, segment.data)
        prior = known.get(segment.reference)
        if prior is None:
            known[segment.reference] = segment
            added.append(segment)
        elif prior != segment:
            raise ValueError(f'segment identity conflict: {segment.reference}')
    return tuple(added)
