from edition.catalog import RevisionCatalog
from intake.layout import validate_label

from .history import IssueHistory
from .manifest import LiteralManifest, assemble_manifest
from .selection import ALL_POSITIONS, PositionSelection, resolve_selection


def issue(
    catalog: RevisionCatalog,
    history: IssueHistory,
    volume: str,
    revision: str,
    station: str,
    selection: PositionSelection = ALL_POSITIONS,
) -> LiteralManifest:
    validate_label(volume, 'volume')
    validate_label(revision, 'revision')
    validate_label(station, 'station')
    frozen = catalog.get(volume, revision)
    if frozen is None:
        raise LookupError('unknown frozen revision')
    positions = resolve_selection(frozen.directory, selection)
    return history.append(lambda packet_id: assemble_manifest(packet_id, station, frozen, positions))
