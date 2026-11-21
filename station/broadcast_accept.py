"""双站共同正文的离线点收。

站与站之间不交换材料：每站只用同卷同版的申报原位中经摘要核验的
底稿，按件中的异或关系唯一恢复本件请求的目标，再登记原字节。
"""

from edition.digests import digest_block
from edition.directory import RevisionDirectory
from intake.layout import validate_label

from shipment.broadcast import (
    BroadcastPacket,
    BroadcastStation,
    decode_broadcast_packet,
    encode_broadcast_packet,
)

from .material import StationBlock, StationMaterial
from .receipts import Receipt, ReceiptLine, ReceiptStore


def _station_request(packet: BroadcastPacket, station: str) -> BroadcastStation:
    for item in packet.stations:
        if item.station == station:
            return item
    raise ValueError('packet is addressed to a different station')


def _known_bottom(
    site: BroadcastStation,
    directory: RevisionDirectory,
    existing: dict[int, StationBlock],
) -> dict[int, bytes]:
    """Only claimed positions with present, well-sized, digest-matching bytes count."""
    known = {}
    for position in site.claims:
        block = existing.get(position)
        if block is None or len(block.data) != directory.width:
            continue
        if digest_block(block.data) != directory.entries[position].digest:
            continue
        known[position] = block.data
    return known


def _reduce(equations: list[tuple[int, int]]) -> dict[int, tuple[int, int]]:
    pivots: dict[int, tuple[int, int]] = {}
    for mask, value in equations:
        while mask:
            pivot = mask.bit_length() - 1
            if pivot in pivots:
                other_mask, other_value = pivots[pivot]
                mask ^= other_mask
                value ^= other_value
            else:
                pivots[pivot] = (mask, value)
                break
        else:
            if value:
                raise ValueError('broadcast frame relations contradict each other')
    for pivot in sorted(pivots, reverse=True):
        row_mask, row_value = pivots[pivot]
        for other in pivots:
            if other != pivot and (pivots[other][0] >> pivot) & 1:
                other_mask, other_value = pivots[other]
                pivots[other] = (other_mask ^ row_mask, other_value ^ row_value)
    return pivots


def _decode_targets(
    packet: BroadcastPacket,
    site: BroadcastStation,
    directory: RevisionDirectory,
    existing: dict[int, StationBlock],
) -> tuple[tuple[int, bytes], ...]:
    known = _known_bottom(site, directory, existing)
    unknown = [position for position in range(directory.slot_count) if position not in known]
    index = {position: offset for offset, position in enumerate(unknown)}
    equations = []
    for frame in packet.frames:
        mask = 0
        value = int.from_bytes(frame.data, 'big')
        for position in frame.positions:
            if position in known:
                value ^= int.from_bytes(known[position], 'big')
            else:
                mask ^= 1 << index[position]
        equations.append((mask, value))
    pivots = _reduce(equations)
    restored = []
    for position in site.wants:
        offset = index[position]
        row = pivots.get(offset)
        if row is None or row[0] != 1 << offset:
            raise ValueError(f'wanted position {position} is underdetermined')
        data = row[1].to_bytes(directory.width, 'big')
        if digest_block(data) != directory.entries[position].digest:
            raise ValueError(f'recovered block digest mismatch at position {position}')
        restored.append((position, data))
    return tuple(restored)


def accept_broadcast(
    material: StationMaterial,
    receipts: ReceiptStore,
    station: str,
    packet: bytes,
) -> Receipt:
    if not isinstance(packet, bytes):
        raise ValueError('expected V2 broadcast packet bytes')
    decoded = decode_broadcast_packet(packet)
    validate_label(station, 'station')
    site = _station_request(decoded, station)
    connection = material.connection
    if receipts.connection is not connection:
        raise ValueError('acceptance requires one station connection')
    directory = decoded.directory
    canonical = encode_broadcast_packet(decoded)
    connection.execute('BEGIN IMMEDIATE')
    with connection:
        known_directory = material.get_directory(station, directory.volume, directory.revision)
        if known_directory is not None and known_directory != directory:
            raise ValueError('packet directory conflicts with station directory')
        prior = receipts.find_packet(station, decoded.packet_id)
        if prior is not None:
            prior_content, first = prior
            if prior_content != canonical:
                raise ValueError('packet ID already bound to different content')
            return first
        existing = material.read_positions(station, directory)
        restored = _decode_targets(decoded, site, directory, existing)
        if known_directory is None:
            material.record_directory(station, directory)
        receipt_id = receipts.reserve_receipt(station, decoded.packet_id, canonical)
        lines = []
        for position, data in restored:
            first = existing.get(position)
            if first is None:
                first = StationBlock(position, data, directory.entries[position].origins, receipt_id)
                material.insert_first(station, directory, first)
            elif first.data != data:
                raise ValueError(f'decoded block conflicts with existing bytes at position {position}')
            lines.append(ReceiptLine(position, first.data, first.origins, first.first_receipt_id))
        receipt = Receipt(receipt_id, station, decoded.packet_id, directory, tuple(lines))
        receipts.save(receipt)
        return receipt
