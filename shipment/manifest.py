from dataclasses import dataclass

from edition.catalog import FrozenRevision
from edition.directory import RevisionDirectory


@dataclass(frozen=True)
class LiteralBlock:
    position: int
    data: bytes


@dataclass(frozen=True)
class LiteralManifest:
    packet_id: str
    station: str
    directory: RevisionDirectory
    blocks: tuple[LiteralBlock, ...]


def assemble_manifest(
    packet_id: str,
    station: str,
    revision: FrozenRevision,
    positions: tuple[int, ...],
) -> LiteralManifest:
    blocks = tuple(LiteralBlock(position, revision.bodies[position]) for position in positions)
    return LiteralManifest(packet_id, station, revision.directory, blocks)
