"""Merge several FIT activity files into a single FIT activity.

The merged activity contains every record from the input files in
chronological order, one lap per input file, and a session and activity
summary so that platforms such as Garmin Connect and Strava accept the file.

Input files are read with the most capable reader that can parse them:
``fit_tool`` first, then ``fitparse``, then a minimal tolerant parser for files
whose definition messages are malformed.

Typical usage:

    merge_fit_files(["morning.fit", "evening.fit"], "merged_activity.fit")
"""

import logging
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pandas as pd
from geopy.distance import geodesic
from fit_tool.fit_file import FitFile
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Activity,
    Event,
    EventType,
    FileType,
    Manufacturer,
    Sport,
    SubSport,
)

logger = logging.getLogger(__name__)

FilePath = str | Path

# --- General constants ------------------------------------------------------

_MILLISECONDS_PER_SECOND = 1000
_SEMICIRCLES_TO_DEGREES = 180.0 / 2**31
_MAX_ABS_DEGREES = 180  # larger magnitudes can only be semicircles
_FIT_EPOCH_OFFSET_SECONDS = 631_065_600  # 1970-01-01 to 1989-12-31 (FIT epoch)
_MIN_STRING_SIZE = 50

_SOURCE_COLUMN = "source"  # index of the input file a record came from
_GPS_COLUMNS = ("position_lat", "position_long")


class DecodedFile(NamedTuple):
    """Record data and sport information extracted from one FIT file.

    Attributes:
        records: One row per record message. ``timestamp`` is in milliseconds
            since the Unix epoch and positions are in degrees.
        sport: Sport reported by the file's session message.
        sub_sport: Sub-sport reported by the file's session message.
    """

    records: pd.DataFrame
    sport: Sport
    sub_sport: SubSport


# --- Small conversion helpers -----------------------------------------------


def _is_number(value: object) -> bool:
    """Return True for ints and floats, excluding booleans."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _to_native(value: float) -> int | float:
    """Convert a number to an int when it is whole, otherwise to a float.

    FIT integer fields reject values such as ``150.0``, but pandas turns every
    numeric column into floats.

    Args:
        value: Any number.

    Returns:
        ``int(value)`` if the value is a whole number, else ``float(value)``.
    """
    value = float(value)
    return int(value) if value.is_integer() else value


def _enum_from_name(enum_cls: type, name: object, default):
    """Look up an enum member by name, ignoring case.

    Args:
        enum_cls: The enum class to search, e.g. ``Sport``.
        name: Member name such as ``"cycling"``.
        default: Value returned when no member has that name.

    Returns:
        The matching member, or ``default``.
    """
    try:
        return enum_cls[str(name).upper()]
    except KeyError:
        return default


def _enum_from_value(enum_cls: type, value: int | None, default):
    """Look up an enum member by its numeric value.

    Args:
        enum_cls: The enum class to search, e.g. ``Sport``.
        value: Numeric value, or None if it was not present in the file.
        default: Value returned when ``value`` is None or unknown.

    Returns:
        The matching member, or ``default``.
    """
    if value is None:
        return default
    try:
        return enum_cls(value)
    except ValueError:
        return default


def _has_gps_columns(records: pd.DataFrame) -> bool:
    """Return True if the DataFrame has both latitude and longitude columns."""
    return set(_GPS_COLUMNS) <= set(records.columns)


# --- Reader 1: fit_tool -----------------------------------------------------


def _numeric_fields(message: RecordMessage) -> dict[str, float]:
    """Extract every valid numeric field of a record message.

    Args:
        message: A decoded ``RecordMessage``.

    Returns:
        Mapping of field name to value; invalid and non-numeric fields are
        left out.
    """
    values = {}
    for field in message.fields:
        if not field.is_valid():
            continue
        value = field.get_value()
        if _is_number(value):
            values[field.name] = value
    return values


def _decode_with_fit_tool(file_path: FilePath) -> DecodedFile:
    """Read a FIT file with ``fit_tool`` (strict, most complete reader).

    Args:
        file_path: Path of the FIT file.

    Returns:
        The decoded records and sport information.

    Raises:
        Exception: Whatever ``fit_tool`` raises for files with malformed
            definitions, such as "Size is not a multiple of type".
    """
    fit = FitFile.from_file(str(file_path))

    rows = []
    sport, sub_sport = Sport.GENERIC, SubSport.GENERIC

    for fit_record in fit.records:
        message = fit_record.message

        if isinstance(message, SessionMessage):
            sport = message.sport if message.sport is not None else sport
            sub_sport = message.sub_sport if message.sub_sport is not None else sub_sport
        elif isinstance(message, RecordMessage):
            row = _numeric_fields(message)
            if "timestamp" in row:
                rows.append(row)

    return DecodedFile(pd.DataFrame(rows), sport, sub_sport)


# --- Reader 2: fitparse -----------------------------------------------------


def _datetime_to_epoch_ms(moment: datetime) -> int:
    """Convert a naive UTC datetime to milliseconds since the Unix epoch."""
    return int(moment.replace(tzinfo=UTC).timestamp() * _MILLISECONDS_PER_SECOND)


def _fitparse_record_row(values: dict) -> dict[str, float]:
    """Convert one ``fitparse`` record into the column conventions used here.

    ``fitparse`` yields datetimes for timestamps and semicircles for
    positions; this converts them to epoch milliseconds and degrees.

    Args:
        values: Field name to value, as returned by ``get_values()``.

    Returns:
        A row of numeric fields, with ``timestamp`` only if one was present.
    """
    row = {}
    for name, value in values.items():
        if name == "timestamp":
            if value is not None:
                row[name] = _datetime_to_epoch_ms(value)
        elif _is_number(value):
            is_semicircles = name.startswith("position_") and abs(value) > _MAX_ABS_DEGREES
            row[name] = value * _SEMICIRCLES_TO_DEGREES if is_semicircles else value
    return row


def _decode_with_fitparse(file_path: FilePath) -> DecodedFile:
    """Read a FIT file with ``fitparse`` (tolerant of unknown messages).

    Args:
        file_path: Path of the FIT file.

    Returns:
        The decoded records and sport information.

    Raises:
        Exception: Whatever ``fitparse`` raises, e.g. ``FitParseError`` for
            impossible field sizes.
    """
    from fitparse import FitFile as ParseFitFile

    rows = []
    sport, sub_sport = Sport.GENERIC, SubSport.GENERIC

    for message in ParseFitFile(str(file_path)).get_messages():
        if message.name == "session":
            values = message.get_values()
            sport = _enum_from_name(Sport, values.get("sport"), sport)
            sub_sport = _enum_from_name(SubSport, values.get("sub_sport"), sub_sport)
        elif message.name == "record":
            row = _fitparse_record_row(message.get_values())
            if "timestamp" in row:
                rows.append(row)

    return DecodedFile(pd.DataFrame(rows), sport, sub_sport)


# --- Reader 3: minimal salvage parser ---------------------------------------
# Some files contain definition messages with impossible field sizes (for
# example a 4-byte uint32 declared as 1 byte). fit_tool and fitparse refuse
# those files. The salvage parser ignores any field it cannot interpret and
# keeps the core record data.

_HEADER_SIZE_OFFSET = 0
_DATA_SIZE_OFFSET = 4  # 4-byte little-endian size of the data section

# message information is stored in the following bits of the header byte
_COMPRESSED_TIMESTAMP_FLAG = 0x80
_DEFINITION_MESSAGE_FLAG = 0x40
_DEVELOPER_DATA_FLAG = 0x20
_LOCAL_TYPE_MASK = 0x0F
_COMPRESSED_LOCAL_TYPE_SHIFT = 5
_COMPRESSED_LOCAL_TYPE_MASK = 0x03
_TIME_OFFSET_MASK = 0x1F
_TIME_OFFSET_ROLLOVER = 32
_ARCHITECTURE_BIG_ENDIAN = 1

# common field numbers - which fields represent which data
_TIMESTAMP_FIELD_NUMBER = 253
_SESSION_SPORT_FIELD = 5
_SESSION_SUB_SPORT_FIELD = 6

# message type numbers
_SESSION_MESSAGE_NUMBER = 18
_RECORD_MESSAGE_NUMBER = 20


class _BaseType(NamedTuple):
    """A FIT base type: how to unpack it and which value means 'invalid'."""

    struct_code: str
    size: int
    invalid_value: int | None

# base types are the type of number a field can represent
_BASE_TYPES = {
    0x00: _BaseType("B", 1, 0xFF),  # enum
    0x01: _BaseType("b", 1, 0x7F),  # sint8
    0x02: _BaseType("B", 1, 0xFF),  # uint8
    0x83: _BaseType("h", 2, 0x7FFF),  # sint16
    0x84: _BaseType("H", 2, 0xFFFF),  # uint16
    0x85: _BaseType("i", 4, 0x7FFFFFFF),  # sint32
    0x86: _BaseType("I", 4, 0xFFFFFFFF),  # uint32
    0x88: _BaseType("f", 4, None),  # float32
    0x89: _BaseType("d", 8, None),  # float64
    0x0A: _BaseType("B", 1, 0x00),  # uint8z
    0x8B: _BaseType("H", 2, 0x0000),  # uint16z
    0x8C: _BaseType("I", 4, 0x00000000),  # uint32z
    0x0D: _BaseType("B", 1, 0xFF),  # byte
    0x8E: _BaseType("q", 8, 0x7FFFFFFFFFFFFFFF),  # sint64
    0x8F: _BaseType("Q", 8, 0xFFFFFFFFFFFFFFFF),  # uint64
    0x90: _BaseType("Q", 8, 0x0000000000000000),  # uint64z
}


class _RecordField(NamedTuple):
    """How a raw record field maps to a column: value = raw / scale - offset."""

    name: str
    scale: int = 1
    offset: int = 0
    semicircles: bool = False


_RECORD_FIELDS = {
    0: _RecordField("position_lat", semicircles=True),
    1: _RecordField("position_long", semicircles=True),
    2: _RecordField("altitude", scale=5, offset=500),
    3: _RecordField("heart_rate"),
    4: _RecordField("cadence"),
    5: _RecordField("distance", scale=100),
    6: _RecordField("speed", scale=1000),
    7: _RecordField("power"),
    13: _RecordField("temperature"),
    73: _RecordField("enhanced_speed", scale=1000),
    78: _RecordField("enhanced_altitude", scale=5, offset=500),
}


class _FieldDefinition(NamedTuple):
    """One field of a definition message."""

    number: int
    size: int
    base_type: int


@dataclass(frozen=True)
class _MessageDefinition:
    """A definition message: the layout of the data messages that follow it.

    Attributes:
        byte_order: ``"<"`` (little endian) or ``">"`` (big endian).
        global_number: Global message number, e.g. 20 for record messages.
        fields: Field layout, in the order the fields appear in the data.
        developer_data_size: Total bytes of developer fields, which are skipped.
    """

    byte_order: str
    global_number: int
    fields: list[_FieldDefinition]
    developer_data_size: int

    @property
    def body_size(self) -> int:
        """Size in bytes of one data message body, excluding its header."""
        return sum(field.size for field in self.fields) + self.developer_data_size


def _read_scalar(raw: bytes, field: _FieldDefinition, byte_order: str) -> int | float | None:
    """Unpack one scalar field value.

    Args:
        raw: The field's bytes.
        field: The field's definition.
        byte_order: ``"<"`` or ``">"``.

    Returns:
        The value, or None if the type is unknown, the field is an array or a
        string, its size does not match its type, or it holds the 'invalid'
        marker.
    """
    base_type = _BASE_TYPES.get(field.base_type)
    if base_type is None or field.size != base_type.size:
        return None
    (value,) = struct.unpack(byte_order + base_type.struct_code, raw)
    if base_type.invalid_value is not None and value == base_type.invalid_value:
        return None
    return value


def _local_message_type(header: int) -> int:
    """Return the local message type encoded in a record header byte."""
    if header & _COMPRESSED_TIMESTAMP_FLAG:
        return (header >> _COMPRESSED_LOCAL_TYPE_SHIFT) & _COMPRESSED_LOCAL_TYPE_MASK
    return header & _LOCAL_TYPE_MASK


class _SalvageParser:
    """Minimal FIT parser that extracts record data and skips what it cannot read.

    It understands definition and data messages, big- and little-endian
    layouts, developer fields and compressed-timestamp headers. Parsing stops
    quietly at truncated data, keeping everything read so far.
    """

    def __init__(self, data: bytes) -> None:
        """Prepare to parse the raw bytes of a FIT file.

        Args:
            data: The complete file contents.
        """
        header_size = data[_HEADER_SIZE_OFFSET]
        data_size = int.from_bytes(data[_DATA_SIZE_OFFSET:_DATA_SIZE_OFFSET + 4], "little")
        self._data = data
        self._position = header_size
        self._end = min(len(data), header_size + data_size)
        self._definitions: dict[int, _MessageDefinition] = {}
        self._last_timestamp: int | None = None  # FIT seconds, for compressed headers
        self._rows: list[dict[str, float]] = []
        self._sport_raw: int | None = None
        self._sub_sport_raw: int | None = None

    def parse(self) -> DecodedFile:
        """Parse the whole file.

        Returns:
            The record rows read, plus the sport and sub-sport if present.
        """
        while self._position < self._end:
            header = self._take_byte()
            if self._is_definition_header(header):
                self._parse_definition_message(header)
            elif not self._parse_data_message(header):
                break  # unknown definition or truncated data

        sport = _enum_from_value(Sport, self._sport_raw, Sport.GENERIC)
        sub_sport = _enum_from_value(SubSport, self._sub_sport_raw, SubSport.GENERIC)
        return DecodedFile(pd.DataFrame(self._rows), sport, sub_sport)

    @staticmethod
    def _is_definition_header(header: int) -> bool:
        """Return True if the header byte starts a definition message."""
        is_compressed = bool(header & _COMPRESSED_TIMESTAMP_FLAG)
        return not is_compressed and bool(header & _DEFINITION_MESSAGE_FLAG)

    def _take_byte(self) -> int:
        """Read the next byte and advance. Raises IndexError past the end."""
        value = self._data[self._position]
        self._position += 1
        return value

    def _take(self, count: int) -> bytes:
        """Read the next ``count`` bytes and advance."""
        chunk = self._data[self._position:self._position + count]
        self._position += count
        return chunk

    def _parse_definition_message(self, header: int) -> None:
        """Read a definition message and remember it under its local type.

        Args:
            header: The already-consumed header byte of the message.
        """
        self._take_byte()  # reserved byte
        byte_order = ">" if self._take_byte() == _ARCHITECTURE_BIG_ENDIAN else "<"
        global_number = int.from_bytes(
            self._take(2), "big" if byte_order == ">" else "little"
        )
        field_count = self._take_byte()
        fields = [self._take_field_definition() for _ in range(field_count)]

        developer_data_size = 0
        if header & _DEVELOPER_DATA_FLAG:
            developer_data_size = self._skip_developer_field_definitions()

        self._definitions[header & _LOCAL_TYPE_MASK] = _MessageDefinition(
            byte_order, global_number, fields, developer_data_size
        )

    def _take_field_definition(self) -> _FieldDefinition:
        """Read one three-byte field definition."""
        number, size, base_type = (self._take_byte() for _ in range(3))
        return _FieldDefinition(number, size, base_type)

    def _skip_developer_field_definitions(self) -> int:
        """Read developer field definitions and return their combined data size."""
        total_size = 0
        for _ in range(self._take_byte()):
            _number, size, _developer_index = (self._take_byte() for _ in range(3))
            total_size += size
        return total_size

    def _parse_data_message(self, header: int) -> bool:
        """Read a data message and collect any record or session fields from it.

        Args:
            header: The already-consumed header byte of the message.

        Returns:
            False if parsing cannot continue (no matching definition, or the
            message is cut off by the end of the file); otherwise True.
        """
        definition = self._definitions.get(_local_message_type(header))
        if definition is None:
            return False

        body_end = self._position + definition.body_size
        if body_end > self._end:
            return False
        body = self._data[self._position:body_end]
        self._position = body_end

        timestamp = None
        if header & _COMPRESSED_TIMESTAMP_FLAG:
            timestamp = self._expand_compressed_timestamp(header)

        record: dict[str, float] = {}
        cursor = 0
        for field in definition.fields:
            raw = body[cursor:cursor + field.size]
            cursor += field.size

            if field.number == _TIMESTAMP_FIELD_NUMBER:
                value = _read_scalar(raw, field, definition.byte_order)
                if value is not None:
                    timestamp = value
                    self._last_timestamp = value
            elif definition.global_number == _RECORD_MESSAGE_NUMBER:
                self._collect_record_field(record, field, raw, definition.byte_order)
            elif definition.global_number == _SESSION_MESSAGE_NUMBER:
                self._collect_session_field(field, raw, definition.byte_order)

        if definition.global_number == _RECORD_MESSAGE_NUMBER and timestamp is not None:
            unix_seconds = timestamp + _FIT_EPOCH_OFFSET_SECONDS
            record["timestamp"] = unix_seconds * _MILLISECONDS_PER_SECOND
            self._rows.append(record)
        return True

    def _expand_compressed_timestamp(self, header: int) -> int | None:
        """Rebuild a full timestamp from a compressed-timestamp header.

        The header carries only the low five bits of the time; the rest comes
        from the last full timestamp seen, allowing for one rollover.

        Args:
            header: A header byte with the compressed-timestamp flag set.

        Returns:
            The timestamp in FIT seconds, or None if no earlier full timestamp
            has been seen.
        """
        if self._last_timestamp is None:
            return None
        offset = header & _TIME_OFFSET_MASK
        base = self._last_timestamp & ~_TIME_OFFSET_MASK
        rolled_over = offset < (self._last_timestamp & _TIME_OFFSET_MASK)
        timestamp = base + offset + (_TIME_OFFSET_ROLLOVER if rolled_over else 0)
        self._last_timestamp = timestamp
        return timestamp

    @staticmethod
    def _collect_record_field(
        record: dict[str, float], field: _FieldDefinition, raw: bytes, byte_order: str
    ) -> None:
        """Add a known record field to ``record``, applying scale and offset."""
        spec = _RECORD_FIELDS.get(field.number)
        if spec is None:
            return
        value = _read_scalar(raw, field, byte_order)
        if value is None:
            return
        if spec.semicircles:
            record[spec.name] = value * _SEMICIRCLES_TO_DEGREES
        else:
            record[spec.name] = value / spec.scale - spec.offset

    def _collect_session_field(
        self, field: _FieldDefinition, raw: bytes, byte_order: str
    ) -> None:
        """Remember the sport or sub-sport from a session message field."""
        if field.number == _SESSION_SPORT_FIELD:
            self._sport_raw = _read_scalar(raw, field, byte_order)
        elif field.number == _SESSION_SUB_SPORT_FIELD:
            self._sub_sport_raw = _read_scalar(raw, field, byte_order)


def _decode_with_salvage_parser(file_path: FilePath) -> DecodedFile:
    """Read a FIT file with the minimal tolerant parser.

    Args:
        file_path: Path of the FIT file.

    Returns:
        The decoded records (core fields only) and sport information.
    """
    return _SalvageParser(Path(file_path).read_bytes()).parse()


_READERS = (
    ("fit_tool", _decode_with_fit_tool),
    ("fitparse", _decode_with_fitparse),
    ("salvage parser", _decode_with_salvage_parser),
)


def decode_fit_records(file_path: FilePath) -> DecodedFile:
    """Read the record messages and sport information from a FIT file.

    Readers are tried from most to least complete: ``fit_tool``, then
    ``fitparse``, then a minimal tolerant parser. The first one that succeeds
    wins, and each failure is logged.

    Args:
        file_path: Path of the FIT file.

    Returns:
        The decoded records and sport information.

    Raises:
        ValueError: If every reader fails on the file.
    """
    for index, (name, reader) in enumerate(_READERS):
        try:
            decoded = reader(file_path)
        except Exception as error:  # each reader raises its own error types
            logger.warning("%s could not read %s: %s", name, file_path, error)
            continue
        if index > 0:
            logger.warning("Read %s using the %s.", file_path, name)
        return decoded

    raise ValueError(f"Could not read any records from {file_path}.")


# --- Distance and position calculations -------------------------------------


def recalculate_distances(records: pd.DataFrame) -> pd.DataFrame:
    """Recalculate cumulative distance in metres from the GPS coordinates.

    No distance is added across a change of source file or across records
    that have no GPS fix, so gaps between files do not count as travelled
    distance.

    Args:
        records: Records with a ``source`` column and, optionally,
            ``position_lat`` and ``position_long`` columns in degrees.

    Returns:
        A copy of ``records`` with a ``distance`` column. Without GPS columns
        the distance is 0 throughout.
    """
    records = records.copy()
    if not _has_gps_columns(records):
        records["distance"] = 0.0
        return records

    distances = []
    cumulative = 0.0
    previous_point = None
    previous_source = None

    coordinates = zip(
        records["position_lat"],
        records["position_long"],
        records[_SOURCE_COLUMN],
        strict=True,
    )
    for lat, lon, source in coordinates:
        if source != previous_source:
            previous_point = None  # new file: do not bridge the gap
            previous_source = source

        if pd.isna(lat) or pd.isna(lon):
            previous_point = None
        else:
            point = (float(lat), float(lon))
            if previous_point is not None:
                cumulative += geodesic(previous_point, point).meters
            previous_point = point
        distances.append(cumulative)

    records["distance"] = distances
    return records


def gps_endpoints(records: pd.DataFrame):
    """Find the earliest and latest GPS points in a set of records.

    Only rows with both latitude and longitude count, so each returned
    coordinate pair comes from a single point.

    Args:
        records: Records with ``timestamp`` and, optionally, GPS columns.

    Returns:
        ``((start_lat, start_lon), (end_lat, end_lon))`` for the points with
        the earliest and latest timestamps, or ``(None, None)`` if there is
        no GPS data.
    """
    if not _has_gps_columns(records):
        return None, None

    gps = records.dropna(subset=list(_GPS_COLUMNS))
    if gps.empty:
        return None, None

    first = gps.loc[gps["timestamp"].idxmin()]
    last = gps.loc[gps["timestamp"].idxmax()]
    return (
        (float(first["position_lat"]), float(first["position_long"])),
        (float(last["position_lat"]), float(last["position_long"])),
    )


# --- Loading and combining the input files ----------------------------------


def _load_sources(file_paths: Sequence[FilePath]) -> DecodedFile:
    """Read every input file and combine their records into one DataFrame.

    Files without record messages are skipped with a warning. The sport comes
    from the first usable file.

    Args:
        file_paths: Paths of the FIT files to read.

    Returns:
        The combined records, tagged with a ``source`` column, and the sport.

    Raises:
        ValueError: If no file contains any record messages.
    """
    frames = []
    sport, sub_sport = Sport.GENERIC, SubSport.GENERIC

    for path in file_paths:
        decoded = decode_fit_records(path)
        if decoded.records.empty:
            logger.warning("No record messages in %s, skipping.", path)
            continue
        if not frames:
            sport, sub_sport = decoded.sport, decoded.sub_sport
        frames.append(decoded.records.assign(**{_SOURCE_COLUMN: len(frames)}))

    if not frames:
        raise ValueError("No record messages found in the FIT files.")

    return DecodedFile(pd.concat(frames, ignore_index=True), sport, sub_sport)


def _sort_and_deduplicate(records: pd.DataFrame) -> pd.DataFrame:
    """Order records chronologically and drop repeated timestamps.

    When several records share a timestamp, the first one is kept.
    """
    return (
        records.sort_values("timestamp")
        .drop_duplicates(subset="timestamp", keep="first")
        .reset_index(drop=True)
    )


# --- Building the merged FIT file -------------------------------------------


def _to_record_message(row: dict) -> RecordMessage:
    """Convert a DataFrame row into a ``RecordMessage``.

    Missing values, the helper ``source`` column, and fields that
    ``RecordMessage`` does not support or cannot accept are skipped.

    Args:
        row: Column name to value, as produced by ``DataFrame.to_dict``.

    Returns:
        A populated record message.
    """
    message = RecordMessage()
    for field, value in row.items():
        if field == _SOURCE_COLUMN or pd.isna(value) or not hasattr(message, field):
            continue
        try:
            setattr(message, field, _to_native(value))
        except (AttributeError, TypeError, ValueError):
            continue  # read-only or unsupported field
    return message


def _elapsed_seconds(start_ms: int, end_ms: int) -> float:
    """Return the seconds between two millisecond timestamps."""
    return (end_ms - start_ms) / _MILLISECONDS_PER_SECOND


def _time_bounds(records: pd.DataFrame) -> tuple[int, int]:
    """Return the earliest and latest timestamps (ms) in the records."""
    return int(records["timestamp"].min()), int(records["timestamp"].max())


def _make_file_id_message(time_created_ms: int) -> FileIdMessage:
    """Create the file-id message that marks the file as an activity."""
    message = FileIdMessage()
    message.type = FileType.ACTIVITY
    message.manufacturer = Manufacturer.DEVELOPMENT.value
    message.product = 0
    message.serial_number = 0
    message.time_created = time_created_ms
    return message


def _make_timer_event(event_type: EventType, timestamp_ms: int) -> EventMessage:
    """Create a timer start or stop event."""
    message = EventMessage()
    message.event = Event.TIMER
    message.event_type = event_type
    message.timestamp = timestamp_ms
    return message


def _make_lap_message(
    segment: pd.DataFrame, lap_index: int, sport: Sport, sub_sport: SubSport
) -> LapMessage:
    """Create the lap summary for the records of one source file.

    Args:
        segment: Records from a single source file, with a ``distance`` column.
        lap_index: Zero-based index of this lap.
        sport: Sport to record on the lap.
        sub_sport: Sub-sport to record on the lap.

    Returns:
        The lap message, with start and end positions when GPS data exists.
    """
    start_ms, end_ms = _time_bounds(segment)
    elapsed = _elapsed_seconds(start_ms, end_ms)

    lap = LapMessage()
    lap.message_index = lap_index
    lap.timestamp = end_ms
    lap.start_time = start_ms
    lap.total_elapsed_time = elapsed
    lap.total_timer_time = elapsed
    lap.total_distance = float(segment["distance"].iloc[-1] - segment["distance"].iloc[0])
    lap.sport = sport
    lap.sub_sport = sub_sport

    start_position, end_position = gps_endpoints(segment)
    if start_position:
        lap.start_position_lat, lap.start_position_long = start_position
    if end_position:
        lap.end_position_lat, lap.end_position_long = end_position
    return lap


def _add_segment(
    builder: FitFileBuilder,
    segment: pd.DataFrame,
    lap_index: int,
    sport: Sport,
    sub_sport: SubSport,
) -> float:
    """Add one source file's timer events, records and lap to the builder.

    Args:
        builder: The file under construction.
        segment: Records from a single source file.
        lap_index: Zero-based index of the lap being added.
        sport: Sport to record on the lap.
        sub_sport: Sub-sport to record on the lap.

    Returns:
        The elapsed seconds of this segment.
    """
    start_ms, end_ms = _time_bounds(segment)

    builder.add(_make_timer_event(EventType.START, start_ms))
    builder.add_all([_to_record_message(row) for row in segment.to_dict("records")])
    builder.add(_make_timer_event(EventType.STOP_ALL, end_ms))
    builder.add(_make_lap_message(segment, lap_index, sport, sub_sport))
    return _elapsed_seconds(start_ms, end_ms)


def _make_session_message(
    records: pd.DataFrame,
    lap_count: int,
    timer_seconds: float,
    sport: Sport,
    sub_sport: SubSport,
) -> SessionMessage:
    """Create the session summary for the whole merged activity.

    The FIT session message has a start position but no end position; the last
    GPS point is stored as the end position of the final lap instead.

    Args:
        records: All merged records, sorted by time, with a ``distance`` column.
        lap_count: Number of laps in the activity.
        timer_seconds: Sum of the segments' elapsed seconds (excludes gaps).
        sport: Sport to record on the session.
        sub_sport: Sub-sport to record on the session.

    Returns:
        The session message.
    """
    start_ms, end_ms = _time_bounds(records)

    session = SessionMessage()
    session.message_index = 0
    session.timestamp = end_ms
    session.start_time = start_ms
    session.total_elapsed_time = _elapsed_seconds(start_ms, end_ms)
    session.total_timer_time = timer_seconds
    session.total_distance = float(records["distance"].iloc[-1])
    session.sport = sport
    session.sub_sport = sub_sport
    session.first_lap_index = 0
    session.num_laps = lap_count

    start_position, _ = gps_endpoints(records)
    if start_position:
        session.start_position_lat, session.start_position_long = start_position
    return session


def _make_activity_message(end_ms: int, timer_seconds: float) -> ActivityMessage:
    """Create the closing activity message, marking a single manual session."""
    activity = ActivityMessage()
    activity.timestamp = end_ms
    activity.total_timer_time = timer_seconds
    activity.num_sessions = 1
    activity.type = Activity.MANUAL
    activity.event = Event.ACTIVITY
    activity.event_type = EventType.STOP
    return activity


def _build_fit_file(records: pd.DataFrame, sport: Sport, sub_sport: SubSport) -> FitFile:
    """Assemble a complete FIT activity from the merged records.

    Args:
        records: Merged records, sorted by time, with ``source`` and
            ``distance`` columns.
        sport: Sport for the laps and session.
        sub_sport: Sub-sport for the laps and session.

    Returns:
        The built FIT file: file id, one segment per source file, then the
        session and activity summaries.
    """
    start_ms, end_ms = _time_bounds(records)
    builder = FitFileBuilder(auto_define=True, min_string_size=_MIN_STRING_SIZE)
    builder.add(_make_file_id_message(start_ms))

    segments = [segment for _, segment in records.groupby(_SOURCE_COLUMN, sort=False)]
    timer_seconds = sum(
        _add_segment(builder, segment, lap_index, sport, sub_sport)
        for lap_index, segment in enumerate(segments)
    )

    builder.add(_make_session_message(records, len(segments), timer_seconds, sport, sub_sport))
    builder.add(_make_activity_message(end_ms, timer_seconds))
    return builder.build()


def merge_fit_files(file_paths: Sequence[FilePath], output_path: FilePath) -> FitFile:
    """Merge several FIT files into one FIT activity and save it.

    Records from all inputs are sorted by time and de-duplicated by timestamp.
    Distance is recalculated from GPS, without counting the gaps between
    files. The result has one lap per input file.

    Args:
        file_paths: Paths of the FIT files to merge.
        output_path: Where to write the merged FIT file.

    Returns:
        The merged FIT file, which has also been written to ``output_path``.

    Raises:
        ValueError: If ``file_paths`` is empty, a file cannot be read by any
            reader, or no file contains record messages.
    """
    if not file_paths:
        raise ValueError("At least one FIT file is required.")

    records, sport, sub_sport = _load_sources(file_paths)
    records = _sort_and_deduplicate(records)
    records = recalculate_distances(records)

    fit_file = _build_fit_file(records, sport, sub_sport)
    fit_file.to_file(str(output_path))
    return fit_file