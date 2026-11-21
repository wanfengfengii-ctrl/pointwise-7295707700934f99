from edition.directory import RevisionDirectory, validate_directory
from intake.layout import validate_label
from station.material import StationMaterial

from .accounting import Statement, build_statement
from .archive import StatementArchive
from .coverage import compute_coverage


def reconcile(
    material: StationMaterial,
    archive: StatementArchive,
    station: str,
    directory: RevisionDirectory,
) -> Statement:
    validate_label(station, 'station')
    validate_directory(directory)
    connection = material.connection
    if archive.connection is not connection:
        raise ValueError('reconciliation requires one station connection')
    connection.execute('BEGIN IMMEDIATE')
    with connection:
        known = material.get_directory(station, directory.volume, directory.revision)
        if known is None:
            raise LookupError('station has not seen this revision')
        if known != directory:
            raise ValueError('requested directory conflicts with station directory')
        positions = material.read_positions(station, known)
        coverage = compute_coverage(known, positions)
        return archive.append(build_statement(station, known, coverage))
