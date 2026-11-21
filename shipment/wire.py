import base64
import binascii
import json

from edition.directory import directory_from_data, directory_to_data
from intake.layout import validate_label

from .manifest import LiteralBlock, LiteralManifest


def encode_literal_packet(packet: LiteralManifest) -> bytes:
    payload = {
        'version': 1,
        'packet_id': packet.packet_id,
        'station': packet.station,
        'directory': directory_to_data(packet.directory),
        'blocks': [
            {'position': block.position, 'data': base64.b64encode(block.data).decode('ascii')}
            for block in packet.blocks
        ],
    }
    return json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError(f'duplicate field: {key}')
        fields[key] = value
    return fields


def decode_literal_packet(wire: bytes) -> LiteralManifest:
    if not isinstance(wire, bytes):
        raise ValueError('expected literal packet bytes')
    try:
        data = json.loads(wire.decode('utf-8'), object_pairs_hook=_unique_fields)
        if not isinstance(data, dict) or set(data) != {'version', 'packet_id', 'station', 'directory', 'blocks'}:
            raise ValueError('invalid literal packet fields')
        if type(data['version']) is not int or data['version'] != 1:
            raise ValueError('unsupported literal packet version')
        validate_label(data['packet_id'], 'packet ID')
        validate_label(data['station'], 'station')
        directory = directory_from_data(data['directory'])
        if not isinstance(data['blocks'], list):
            raise ValueError('expected an ordered list of literal blocks')
        blocks = []
        for item in data['blocks']:
            if not isinstance(item, dict) or set(item) != {'position', 'data'}:
                raise ValueError('invalid literal block fields')
            if type(item['position']) is not int or not isinstance(item['data'], str):
                raise ValueError('invalid literal block representation')
            blocks.append(LiteralBlock(item['position'], base64.b64decode(item['data'], validate=True)))
    except (UnicodeError, binascii.Error, RecursionError) as error:
        raise ValueError('invalid literal packet encoding') from error
    return LiteralManifest(data['packet_id'], data['station'], directory, tuple(blocks))
