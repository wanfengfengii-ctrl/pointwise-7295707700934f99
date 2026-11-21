from dataclasses import dataclass

from edition.digests import digest_block
from edition.directory import validate_directory
from intake.layout import validate_block_width, validate_label, validate_position
from shipment.manifest import LiteralBlock, LiteralManifest


@dataclass(frozen=True)
class VerifiedLiteral:
    manifest: LiteralManifest


def verify_literal_packet(station: str, packet: LiteralManifest) -> VerifiedLiteral:
    validate_label(station, 'station')
    if not isinstance(packet, LiteralManifest):
        raise ValueError('expected a V1 literal packet')
    validate_label(packet.station, 'packet station')
    validate_label(packet.packet_id, 'packet ID')
    if packet.station != station:
        raise ValueError('packet addressed to a different station')
    directory = packet.directory
    validate_directory(directory)
    if not isinstance(packet.blocks, tuple):
        raise ValueError('expected an ordered tuple of literal blocks')
    layout = directory.layout
    seen = set()
    for block in packet.blocks:
        if not isinstance(block, LiteralBlock):
            raise ValueError('expected a literal block')
        validate_position(layout, block.position)
        validate_block_width(layout, block.data)
        if block.position in seen:
            raise ValueError(f'duplicate packet position: {block.position}')
        if digest_block(block.data) != directory.entries[block.position].digest:
            raise ValueError(f'block digest mismatch at position {block.position}')
        seen.add(block.position)
    return VerifiedLiteral(packet)
