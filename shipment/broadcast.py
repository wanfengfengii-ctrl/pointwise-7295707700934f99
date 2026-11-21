"""双站广播补送的规划、线格式和编码。

一帧正文是某个原块，或若干不同原位原块的逐字节异或。发行只选择
一个两站都能按各自申报唯一解码的帧集合，台架额度只计算正文帧。
"""

import itertools
import json
from dataclasses import dataclass

from edition.directory import RevisionDirectory, directory_from_data, directory_to_data
from intake.layout import validate_label, validate_position

from .history import IssueHistory
from .wire import _unique_fields

# 广播规划最多覆盖六个原位。
MAX_POSITIONS = 6
_HEX_DIGITS = frozenset('0123456789abcdef')


@dataclass(frozen=True)
class BroadcastStation:
    station: str
    claims: tuple[int, ...]
    wants: tuple[int, ...]


@dataclass(frozen=True)
class BroadcastFrame:
    positions: tuple[int, ...]
    data: bytes


@dataclass(frozen=True)
class BroadcastPacket:
    packet_id: str
    directory: RevisionDirectory
    frame_budget: int
    stations: tuple[BroadcastStation, ...]
    frames: tuple[BroadcastFrame, ...]


def encode_broadcast_packet(packet: BroadcastPacket) -> bytes:
    payload = {
        'version': 2,
        'packet_id': packet.packet_id,
        'directory': directory_to_data(packet.directory),
        'frame_budget': packet.frame_budget,
        'stations': [
            {
                'station': item.station,
                'claims': list(item.claims),
                'wants': list(item.wants),
            }
            for item in packet.stations
        ],
        'frames': [
            {'positions': list(frame.positions), 'data': frame.data.hex()}
            for frame in packet.frames
        ],
    }
    return json.dumps(
        payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False
    ).encode('ascii')


def _decode_stations(value: object, directory: RevisionDirectory) -> tuple[BroadcastStation, ...]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('broadcast packet requires exactly two stations')
    layout = directory.layout
    stations = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'station', 'claims', 'wants'}:
            raise ValueError('invalid broadcast station fields')
        validate_label(item['station'], 'station')
        claims = item['claims']
        wants = item['wants']
        if not isinstance(claims, list) or not isinstance(wants, list):
            raise ValueError('broadcast claims and wants must be ordered arrays')
        positions = []
        previous = -1
        for position in claims:
            if type(position) is not int:
                raise ValueError('broadcast claims require integer positions')
            validate_position(layout, position)
            if position <= previous:
                raise ValueError('broadcast claims must be in ascending position order')
            previous = position
            positions.append(position)
        ordered = []
        seen = set()
        for position in wants:
            if type(position) is not int:
                raise ValueError('broadcast wants require integer positions')
            validate_position(layout, position)
            if position in seen:
                raise ValueError(f'duplicate wanted position: {position}')
            seen.add(position)
            ordered.append(position)
        if seen.intersection(positions):
            raise ValueError('claimed and wanted positions must not overlap')
        stations.append(BroadcastStation(item['station'], tuple(positions), tuple(ordered)))
    if stations[0].station == stations[1].station:
        raise ValueError('broadcast stations must be distinct')
    return tuple(stations)


def _decode_frames(value: object, directory: RevisionDirectory) -> tuple[BroadcastFrame, ...]:
    if not isinstance(value, list):
        raise ValueError('broadcast frames must be an ordered array')
    layout = directory.layout
    frames = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'positions', 'data'}:
            raise ValueError('invalid broadcast frame fields')
        positions = item['positions']
        data = item['data']
        if not isinstance(positions, list) or not positions:
            raise ValueError('a broadcast frame requires at least one position')
        if not isinstance(data, str):
            raise ValueError('a broadcast frame requires hexadecimal body text')
        ordered = []
        seen = set()
        for position in positions:
            if type(position) is not int:
                raise ValueError('broadcast frame positions must be integers')
            validate_position(layout, position)
            if position in seen:
                raise ValueError(f'duplicate frame position: {position}')
            seen.add(position)
            ordered.append(position)
        if len(data) != 2 * directory.width or set(data) - _HEX_DIGITS:
            raise ValueError('broadcast frame body must be lowercase hexadecimal of one block width')
        frames.append(BroadcastFrame(tuple(ordered), bytes.fromhex(data)))
    return tuple(frames)


def decode_broadcast_packet(wire: bytes) -> BroadcastPacket:
    if not isinstance(wire, bytes):
        raise ValueError('expected broadcast packet bytes')
    try:
        text = wire.decode('utf-8')
    except UnicodeError as error:
        raise ValueError('invalid broadcast packet encoding') from error
    try:
        data = json.loads(text, object_pairs_hook=_unique_fields)
    except RecursionError as error:
        raise ValueError('invalid broadcast packet encoding') from error
    if not isinstance(data, dict) or set(data) != {
        'version', 'packet_id', 'directory', 'frame_budget', 'stations', 'frames'
    }:
        raise ValueError('invalid broadcast packet fields')
    if type(data['version']) is not int or data['version'] != 2:
        raise ValueError('unsupported broadcast packet version')
    validate_label(data['packet_id'], 'packet ID')
    directory = directory_from_data(data['directory'])
    budget = data['frame_budget']
    if type(budget) is not int or budget < 0:
        raise ValueError('frame budget must be a nonnegative integer')
    stations = _decode_stations(data['stations'], directory)
    frames = _decode_frames(data['frames'], directory)
    if len(frames) > budget:
        raise ValueError('broadcast packet exceeds its frame budget')
    return BroadcastPacket(data['packet_id'], directory, budget, stations, frames)


# --- 规划 -----------------------------------------------------------------

def _claim_positions(value: object, directory: RevisionDirectory, label: str) -> frozenset[int]:
    if isinstance(value, (set, frozenset)):
        items = list(value)
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError(f'{label} must be a set or an ordered list of positions')
    seen = set()
    for position in items:
        if type(position) is not int:
            raise ValueError(f'{label} require integer positions')
        validate_position(directory.layout, position)
        if position in seen:
            raise ValueError(f'duplicate claimed position: {position}')
        seen.add(position)
    return frozenset(seen)


def _wanted_positions(value: object, directory: RevisionDirectory, label: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f'{label} must be an ordered list of positions')
    ordered = []
    seen = set()
    for position in value:
        if type(position) is not int:
            raise ValueError(f'{label} require integer positions')
        validate_position(directory.layout, position)
        if position in seen:
            raise ValueError(f'duplicate wanted position: {position}')
        seen.add(position)
        ordered.append(position)
    return tuple(ordered)


def _canonical_basis(vectors) -> tuple[int, ...]:
    """Return the unique reduced row-echelon basis of the spanned subspace."""
    rows: dict[int, int] = {}
    for vector in vectors:
        value = vector
        while value:
            pivot = value.bit_length() - 1
            existing = rows.get(pivot)
            if existing is not None:
                value ^= existing
                continue
            for other in rows:
                if rows[other] >> pivot & 1:
                    rows[other] ^= value
            rows[pivot] = value
            break
    return tuple(rows[pivot] for pivot in sorted(rows))


def _in_span(basis: tuple[int, ...], vector: int) -> bool:
    rows = {row.bit_length() - 1: row for row in basis}
    value = vector
    while value:
        pivot = value.bit_length() - 1
        row = rows.get(pivot)
        if row is None:
            return False
        value ^= row
    return True


def _all_subspaces(count: int):
    """Yield every subspace of GF(2)^count once, by dimension then RREF basis."""
    for dimension in range(0, count + 1):
        for pivots in itertools.combinations(range(count), dimension):
            pivot_set = set(pivots)
            free_columns = [column for column in range(count) if column not in pivot_set]
            free_cells = [
                (row, column)
                for row, pivot in enumerate(pivots)
                for column in free_columns
                if column > pivot
            ]
            for bits in itertools.product((0, 1), repeat=len(free_cells)):
                extras = [0] * dimension
                for (row, column), bit in zip(free_cells, bits):
                    if bit:
                        extras[row] |= 1 << column
                yield dimension, tuple((1 << pivots[row]) | extras[row] for row in range(dimension))


def _determines_all(basis: tuple[int, ...], known: int, targets: tuple[int, ...]) -> bool:
    # Restrict each frame relation to positions unknown to this station.
    projection = _canonical_basis(tuple(vector & ~known for vector in basis))
    for target in targets:
        if known >> target & 1:
            return False
        if not _in_span(projection, 1 << target):
            return False
    return True


def _find_frames(count: int, known: int, targets: tuple[int, ...], budget: int) -> tuple[int, ...] | None:
    limit = min(budget, count)
    for dimension, basis in _all_subspaces(count):
        if dimension > limit:
            break
        if not basis:
            if targets[0] or targets[1]:
                continue
            return basis
        if _determines_all(basis, known[0], targets[0]) and _determines_all(basis, known[1], targets[1]):
            return basis
    return None


def _frame_body(bodies: tuple[bytes, ...], mask: int, width: int) -> bytes:
    value = 0
    for position, body in enumerate(bodies):
        if mask >> position & 1:
            value ^= int.from_bytes(body, 'big')
    return value.to_bytes(width, 'big')


def plan_broadcast(
    catalog,
    history: IssueHistory,
    volume: str,
    revision: str,
    station_a: str,
    claims_a,
    wants_a,
    station_b: str,
    claims_b,
    wants_b,
    frame_budget: int,
) -> bytes | str:
    validate_label(volume, 'volume')
    validate_label(revision, 'revision')
    validate_label(station_a, 'station A')
    validate_label(station_b, 'station B')
    if station_a == station_b:
        raise ValueError('broadcast stations must be distinct')
    frozen = catalog.get(volume, revision)
    if frozen is None:
        raise LookupError('unknown frozen revision')
    directory = frozen.directory
    if directory.slot_count > MAX_POSITIONS:
        raise ValueError('broadcast planning supports at most six positions')
    claimed_a = _claim_positions(claims_a, directory, 'claims A')
    claimed_b = _claim_positions(claims_b, directory, 'claims B')
    wanted_a = _wanted_positions(wants_a, directory, 'wants A')
    wanted_b = _wanted_positions(wants_b, directory, 'wants B')
    if claimed_a.intersection(wanted_a) or claimed_b.intersection(wanted_b):
        raise ValueError('claimed and wanted positions must not overlap')
    if type(frame_budget) is not int or frame_budget < 0:
        raise ValueError('frame budget must be a nonnegative integer')
    mask_a = 0
    for position in claimed_a:
        mask_a |= 1 << position
    mask_b = 0
    for position in claimed_b:
        mask_b |= 1 << position
    basis = _find_frames(
        directory.slot_count, (mask_a, mask_b), (wanted_a, wanted_b), frame_budget
    )
    if basis is None:
        return 'infeasible'
    frames = tuple(
        BroadcastFrame(
            tuple(position for position in range(directory.slot_count) if mask >> position & 1),
            _frame_body(frozen.bodies, mask, directory.width),
        )
        for mask in basis
    )
    stations = (
        BroadcastStation(station_a, tuple(sorted(claimed_a)), wanted_a),
        BroadcastStation(station_b, tuple(sorted(claimed_b)), wanted_b),
    )

    def make_wire(packet_id: str) -> bytes:
        return encode_broadcast_packet(
            BroadcastPacket(packet_id, directory, frame_budget, stations, frames)
        )

    return history.append_wire(make_wire)
