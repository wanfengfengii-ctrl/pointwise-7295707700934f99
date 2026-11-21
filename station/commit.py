from .material import StationBlock, StationMaterial
from .receipts import Receipt, ReceiptLine, ReceiptStore
from .verification import VerifiedLiteral


def commit_acceptance(
    material: StationMaterial,
    receipts: ReceiptStore,
    verified: VerifiedLiteral,
    canonical: bytes,
) -> Receipt:
    connection = material.connection
    if receipts.connection is not connection:
        raise ValueError('acceptance requires one station connection')
    packet = verified.manifest
    station = packet.station
    directory = packet.directory
    connection.execute('BEGIN IMMEDIATE')
    with connection:
        known = material.get_directory(station, directory.volume, directory.revision)
        if known is not None and known != directory:
            raise ValueError('packet directory conflicts with station directory')
        prior = receipts.find_packet(station, packet.packet_id)
        if prior is not None:
            prior_content, first = prior
            if prior_content != canonical:
                raise ValueError('packet ID already bound to different content')
            return first
        if known is None:
            material.record_directory(station, directory)
        receipt_id = receipts.reserve_receipt(station, packet.packet_id, canonical)
        existing = material.read_positions(station, directory)
        lines = []
        for block in packet.blocks:
            position = block.position
            first = existing.get(position)
            if first is None:
                first = StationBlock(position, block.data, directory.entries[position].origins, receipt_id)
                material.insert_first(station, directory, first)
            elif first.data != block.data:
                raise ValueError(f'packet conflicts with existing bytes at position {position}')
            lines.append(ReceiptLine(position, first.data, first.origins, first.first_receipt_id))
        receipt = Receipt(receipt_id, station, packet.packet_id, directory, tuple(lines))
        receipts.save(receipt)
        return receipt
