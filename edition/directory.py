from dataclasses import asdict, dataclass, replace

from intake.layout import DraftLayout, validate_block_width, validate_label, validate_layout
from intake.placement import PlacedBlock

from .digests import digest_block, digest_revision


@dataclass(frozen=True)
class FrozenOrigin:
    kind: str
    reference: str


@dataclass(frozen=True)
class DirectoryEntry:
    position: int
    digest: str
    origins: tuple[FrozenOrigin, ...]


@dataclass(frozen=True)
class RevisionDirectory:
    volume: str
    revision: str
    slot_count: int
    width: int
    total_bytes: int
    entries: tuple[DirectoryEntry, ...]
    digest: str

    @property
    def layout(self) -> DraftLayout:
        return DraftLayout(self.volume, self.slot_count, self.width)


def directory_to_data(directory: RevisionDirectory) -> dict:
    return asdict(directory)


def _identity_fields(directory: RevisionDirectory) -> dict:
    fields = directory_to_data(directory)
    del fields['digest']
    return fields


def _validate_entries(directory: RevisionDirectory) -> None:
    if not isinstance(directory.entries, tuple) or len(directory.entries) != directory.slot_count:
        raise ValueError('directory must have exactly one entry per position')
    for position, entry in enumerate(directory.entries):
        if not isinstance(entry, DirectoryEntry) or type(entry.position) is not int or entry.position != position:
            raise ValueError('directory entries must follow original position order')
        if not isinstance(entry.digest, str) or len(entry.digest) != 64 or set(entry.digest) - set('0123456789abcdef'):
            raise ValueError('invalid block digest')
        if not isinstance(entry.origins, tuple) or not entry.origins:
            raise ValueError('directory entry requires a source chain')
        for index, origin in enumerate(entry.origins):
            kinds = {'device'} if index == 0 else {'correction', 'inheritance'}
            if not isinstance(origin, FrozenOrigin) or not isinstance(origin.kind, str) or origin.kind not in kinds:
                raise ValueError('invalid frozen origin')
            validate_label(origin.reference, 'origin reference')


def validate_directory(directory: RevisionDirectory) -> None:
    if not isinstance(directory, RevisionDirectory):
        raise ValueError('expected a frozen revision directory')
    validate_layout(directory.layout)
    validate_label(directory.revision, 'revision')
    if type(directory.total_bytes) is not int or directory.total_bytes != directory.slot_count * directory.width:
        raise ValueError('directory byte count does not match the layout')
    _validate_entries(directory)
    if directory.digest != digest_revision(_identity_fields(directory)):
        raise ValueError('revision directory digest mismatch')


def build_directory(layout: DraftLayout, revision: str, blocks: tuple[PlacedBlock, ...]) -> RevisionDirectory:
    validate_layout(layout)
    validate_label(revision, 'revision')
    ordered = sorted(blocks, key=lambda block: block.position)
    entries = []
    total_bytes = 0
    for block in ordered:
        validate_block_width(layout, block.data)
        total_bytes += len(block.data)
        entries.append(DirectoryEntry(
            block.position,
            digest_block(block.data),
            tuple(FrozenOrigin(origin.kind, origin.reference) for origin in block.origins),
        ))
    directory = RevisionDirectory(layout.volume, revision, layout.slot_count, layout.width, total_bytes, tuple(entries), '')
    _validate_entries(directory)
    return replace(directory, digest=digest_revision(_identity_fields(directory)))


def origins_from_data(items: list) -> tuple[FrozenOrigin, ...]:
    if not isinstance(items, list):
        raise ValueError('expected an ordered origin list')
    return tuple(FrozenOrigin(**item) for item in items)


def directory_from_data(data: dict) -> RevisionDirectory:
    try:
        if not isinstance(data, dict) or not isinstance(data.get('entries'), list):
            raise ValueError('expected directory fields and an ordered entry list')
        entries = []
        for item in data['entries']:
            if not isinstance(item, dict):
                raise ValueError('expected directory entry fields')
            entries.append(DirectoryEntry(**{**item, 'origins': origins_from_data(item['origins'])}))
        directory = RevisionDirectory(**{**data, 'entries': tuple(entries)})
    except (KeyError, TypeError) as error:
        raise ValueError('invalid directory representation') from error
    validate_directory(directory)
    return directory
