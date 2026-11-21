from dataclasses import dataclass


@dataclass(frozen=True)
class DraftLayout:
    volume: str
    slot_count: int
    width: int


def validate_label(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{field} must be nonempty text')
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise ValueError(f'{field} must be valid UTF-8 text') from error


def validate_layout(layout: DraftLayout) -> None:
    if not isinstance(layout, DraftLayout):
        raise ValueError('expected a draft layout')
    validate_label(layout.volume, 'volume')
    if type(layout.slot_count) is not int or layout.slot_count < 0:
        raise ValueError('slot count must be a nonnegative integer')
    if type(layout.width) is not int or layout.width < 1:
        raise ValueError('block width must be a positive integer')


def validate_position(layout: DraftLayout, position: int) -> None:
    if type(position) is not int or not 0 <= position < layout.slot_count:
        raise ValueError(f'position outside layout: {position!r}')


def validate_block_width(layout: DraftLayout, data: bytes) -> None:
    if not isinstance(data, bytes) or len(data) != layout.width:
        raise ValueError(f'expected an original block of {layout.width} bytes')
