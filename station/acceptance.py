from shipment.manifest import LiteralManifest
from shipment.wire import decode_literal_packet, encode_literal_packet

from .commit import commit_acceptance
from .material import StationMaterial
from .receipts import Receipt, ReceiptStore
from .verification import verify_literal_packet


def accept(
    material: StationMaterial,
    receipts: ReceiptStore,
    station: str,
    packet: LiteralManifest | bytes,
) -> Receipt:
    manifest = decode_literal_packet(packet) if isinstance(packet, bytes) else packet
    verified = verify_literal_packet(station, manifest)
    return commit_acceptance(material, receipts, verified, encode_literal_packet(manifest))
