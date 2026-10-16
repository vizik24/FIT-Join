'''
Tests for merge_fit.py
'''

import enum
import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd
from fit_tool.profile.profile_type import Event, EventType, FileType, Sport, SubSport

import merge_fit


class FakeField:
    '''Stand-in for a fit_tool field.'''
    def __init__(self, name, value, valid=True):
        self.name = name
        self._value = value
        self._valid = valid

    def is_valid(self):
        return self._valid

    def get_value(self):
        return self._value


class FakeMessage:
    '''Stand-in for a fit_tool message.'''
    def __init__(self, fields):
        self.fields = fields


def make_records(timestamps, **columns):
    '''Build a small records DataFrame for the tests.'''
    return pd.DataFrame({'timestamp': timestamps, **columns})


class test_merge_fit(unittest.TestCase):

    class Colour(enum.Enum):
        '''Small enum used only by the tests below.'''
        RED = 1
        BLUE = 2

    # --- Small conversion helpers ---

    def test_is_number_int(self):
        '''
        Function to test _is_number. Works by passing an int and a float to the function.
        '''

        # test int
        result = merge_fit._is_number(1)
        expected = True
        self.assertEqual(result, expected)

        # test float
        result = merge_fit._is_number(1.11)
        expected = True
        self.assertEqual(result, expected)

        # test string
        result = merge_fit._is_number('1')
        expected = False
        self.assertEqual(result, expected)

    def test_to_native(self):
        '''
        Test _to_native. Passes whole float and a fractional float. Checks type as well as value.
        '''
        # assert on type too: 150 == 150.0, so value alone wouldn't catch a float
        result = merge_fit._to_native(150.0)
        self.assertIs(type(result), int)
        self.assertEqual(result, 150)

        result = merge_fit._to_native(1.5)
        self.assertIs(type(result), float)
        self.assertEqual(result, 1.5)

    def test_enum_from_name(self):
        '''
        Function to test _enum_from_name. Works by passing a name in mixed case
        (expects the member) and an unknown name (expects the default).
        '''

        # lookup ignores case
        result = merge_fit._enum_from_name(self.Colour, 'red', None)
        self.assertIs(result, self.Colour.RED)

        # unknown name returns default
        result = merge_fit._enum_from_name(self.Colour, 'green', 'fallback')
        self.assertEqual(result, 'fallback')

    def test_enum_from_value(self):
        '''
        Function to test _enum_from_value. Works by passing a valid value (expects
        the member), an unknown value and None (both expect the default).
        '''

        # valid value returns member
        result = merge_fit._enum_from_value(self.Colour, 2, None)
        self.assertIs(result, self.Colour.BLUE)

        # unknown value returns default
        result = merge_fit._enum_from_value(self.Colour, 99, 'fallback')
        self.assertEqual(result, 'fallback')

        # None returns default
        result = merge_fit._enum_from_value(self.Colour, None, 'fallback')
        self.assertEqual(result, 'fallback')

    def test_has_gps_columns(self):
        '''
        Function to test _has_gps_columns. Works by passing a DataFrame with the
        GPS columns (expects True) and one without them (expects False).
        '''

        # both GPS columns present
        df = pd.DataFrame(columns=list(merge_fit._GPS_COLUMNS) + ['heart_rate'])
        self.assertTrue(merge_fit._has_gps_columns(df))

        # GPS columns missing
        df = pd.DataFrame(columns=['heart_rate'])
        self.assertFalse(merge_fit._has_gps_columns(df))

    # --- Reader 1 and 2 helpers ---

    def test_numeric_fields(self):
        '''
        Function to test _numeric_fields. Works by passing a message with a valid
        number, an invalid field and a string (expects only the valid number).
        '''
        message = FakeMessage([
            FakeField('heart_rate', 150),
            FakeField('bad', 1, valid=False),
            FakeField('name', 'abc'),
        ])
        self.assertEqual(merge_fit._numeric_fields(message), {'heart_rate': 150})

    def test_datetime_to_epoch_ms(self):
        '''
        Function to test _datetime_to_epoch_ms. Works by passing one second after
        the Unix epoch (expects 1000 ms).
        '''
        result = merge_fit._datetime_to_epoch_ms(datetime(1970, 1, 1, 0, 0, 1))
        self.assertEqual(result, 1000)

    def test_fitparse_record_row(self):
        '''
        Function to test _fitparse_record_row. Works by passing a timestamp, a
        semicircle position, a degree position and a string (expects ms, degrees
        and the string dropped), then a row with no timestamp.
        '''
        values = {
            'timestamp': datetime(1970, 1, 1, 0, 0, 1),
            'heart_rate': 150,
            'position_lat': 2**30,  # 90 degrees in semicircles
            'position_long': 10.0,  # already degrees
            'note': 'x',
        }
        result = merge_fit._fitparse_record_row(values)
        expected = {'timestamp': 1000, 'heart_rate': 150,
                    'position_lat': 90.0, 'position_long': 10.0}
        self.assertEqual(result, expected)

        # missing timestamp is left out
        result = merge_fit._fitparse_record_row({'timestamp': None})
        self.assertNotIn('timestamp', result)

    # --- Reader 3: salvage parser ---

    def test_local_message_type(self):
        '''
        Function to test _local_message_type. Works by passing a normal header, a
        definition header and a compressed-timestamp header.
        '''
        self.assertEqual(merge_fit._local_message_type(0x03), 3)
        self.assertEqual(merge_fit._local_message_type(0x41), 1)
        self.assertEqual(merge_fit._local_message_type(0xC5), 2)

    def test_read_scalar(self):
        '''
        Function to test _read_scalar. Works by reading a valid uint8, the invalid
        marker, a size mismatch, an unknown type and both byte orders of a uint16.
        '''
        uint8 = merge_fit._FieldDefinition(3, 1, 0x02)
        self.assertEqual(merge_fit._read_scalar(b'\x96', uint8, '<'), 150)
        self.assertIsNone(merge_fit._read_scalar(b'\xff', uint8, '<'))

        wrong_size = merge_fit._FieldDefinition(3, 1, 0x86)  # uint32 declared as 1 byte
        self.assertIsNone(merge_fit._read_scalar(b'\x01', wrong_size, '<'))

        unknown = merge_fit._FieldDefinition(3, 1, 0x7E)
        self.assertIsNone(merge_fit._read_scalar(b'\x01', unknown, '<'))

        uint16 = merge_fit._FieldDefinition(3, 2, 0x84)
        self.assertEqual(merge_fit._read_scalar(b'\x01\x00', uint16, '<'), 1)
        self.assertEqual(merge_fit._read_scalar(b'\x01\x00', uint16, '>'), 256)

    def test_salvage_parser(self):
        '''
        Function to test _SalvageParser. Works by parsing a tiny hand-built file
        with one definition message and one record (expects the timestamp
        converted to Unix ms and the heart rate read).
        '''
        definition = (
            bytes([0x40, 0, 0]) + struct.pack('<H', 20)  # record message, little endian
            + bytes([2])                                  # two fields
            + bytes([253, 4, 0x86])                       # timestamp, uint32
            + bytes([3, 1, 0x02])                         # heart_rate, uint8
        )
        data_message = bytes([0x00]) + struct.pack('<I', 1000) + bytes([150])
        body = definition + data_message
        header = bytes([12, 0x10]) + struct.pack('<H', 0) + struct.pack('<I', len(body)) + b'.FIT'

        decoded = merge_fit._SalvageParser(header + body).parse()

        self.assertEqual(len(decoded.records), 1)
        expected_ms = (1000 + merge_fit._FIT_EPOCH_OFFSET_SECONDS) * 1000
        self.assertEqual(decoded.records['timestamp'].iloc[0], expected_ms)
        self.assertEqual(decoded.records['heart_rate'].iloc[0], 150)
        self.assertEqual(decoded.sport, Sport.GENERIC)

    def test_decode_fit_records(self):
        '''
        Function to test decode_fit_records. Works by patching the readers: the
        first fails and the second succeeds (expects the second result), then all
        fail (expects ValueError).
        '''
        good = merge_fit.DecodedFile(make_records([1]), Sport.CYCLING, SubSport.GENERIC)

        def failing_reader(path):
            raise RuntimeError('cannot read')

        with mock.patch.object(merge_fit, '_READERS',
                               (('bad', failing_reader), ('good', lambda path: good))):
            with self.assertLogs(merge_fit.logger, level='WARNING'):
                result = merge_fit.decode_fit_records('x.fit')
        self.assertIs(result, good)

        with mock.patch.object(merge_fit, '_READERS', (('bad', failing_reader),)):
            with self.assertLogs(merge_fit.logger, level='WARNING'):
                with self.assertRaises(ValueError):
                    merge_fit.decode_fit_records('x.fit')

    # --- Distance and position ---

    def test_recalculate_distances(self):
        '''
        Function to test recalculate_distances. Works by passing three points where
        the last is from a new source file (expects about 111 m between the first
        two and no distance added across the file change), then records without GPS
        (expects 0).
        '''
        df = make_records([1, 2, 3], position_lat=[0.0, 0.0, 0.0],
                          position_long=[0.0, 0.001, 0.002], source=[0, 0, 1])
        result = merge_fit.recalculate_distances(df)
        self.assertEqual(result['distance'].iloc[0], 0)
        self.assertAlmostEqual(result['distance'].iloc[1], 111, delta=2)
        self.assertEqual(result['distance'].iloc[2], result['distance'].iloc[1])

        df = make_records([1, 2], source=[0, 0])
        result = merge_fit.recalculate_distances(df)
        self.assertTrue((result['distance'] == 0).all())

    def test_gps_endpoints(self):
        '''
        Function to test gps_endpoints. Works by passing unordered GPS records
        (expects the earliest and latest points), then records with no GPS data
        (expects (None, None)).
        '''
        df = make_records([3, 1, 2], position_lat=[3.0, 1.0, 2.0],
                          position_long=[30.0, 10.0, 20.0])
        self.assertEqual(merge_fit.gps_endpoints(df), ((1.0, 10.0), (3.0, 30.0)))

        self.assertEqual(merge_fit.gps_endpoints(make_records([1])), (None, None))

        empty_gps = make_records([1], position_lat=[None], position_long=[None])
        self.assertEqual(merge_fit.gps_endpoints(empty_gps), (None, None))

    # --- Loading and combining ---

    def test_load_sources(self):
        '''
        Function to test _load_sources. Works by patching the decoder so the first
        file has records and the second is empty (expects one tagged frame and the
        first file's sport), then only empty files (expects ValueError).
        '''
        full = merge_fit.DecodedFile(make_records([1]), Sport.CYCLING, SubSport.GENERIC)
        empty = merge_fit.DecodedFile(pd.DataFrame(), Sport.GENERIC, SubSport.GENERIC)

        with mock.patch.object(merge_fit, 'decode_fit_records', side_effect=[full, empty]):
            with self.assertLogs(merge_fit.logger, level='WARNING'):
                records, sport, _ = merge_fit._load_sources(['a.fit', 'b.fit'])
        self.assertEqual(len(records), 1)
        self.assertIn('source', records.columns)
        self.assertEqual(sport, Sport.CYCLING)

        with mock.patch.object(merge_fit, 'decode_fit_records', return_value=empty):
            with self.assertLogs(merge_fit.logger, level='WARNING'):
                with self.assertRaises(ValueError):
                    merge_fit._load_sources(['a.fit'])

    def test_sort_and_deduplicate(self):
        '''
        Function to test _sort_and_deduplicate. Works by passing unordered records
        with a repeated timestamp (expects sorted, unique timestamps).
        '''
        df = make_records([3, 1, 1, 2], heart_rate=[30, 10, 11, 20])
        result = merge_fit._sort_and_deduplicate(df)
        self.assertEqual(list(result['timestamp']), [1, 2, 3])

    # --- Building the FIT file ---

    def test_to_record_message(self):
        '''
        Function to test _to_record_message. Works by passing a row with a valid
        field, a NaN, the source column and an unknown field (expects only the
        valid field to be set).
        '''
        row = {'heart_rate': 150.0, 'speed': float('nan'), 'source': 0, 'not_a_field': 5}
        message = merge_fit._to_record_message(row)
        self.assertEqual(message.heart_rate, 150)
        self.assertIsNone(message.speed)
        self.assertFalse(hasattr(message, 'not_a_field'))

    def test_elapsed_seconds_and_time_bounds(self):
        '''
        Functions to test _elapsed_seconds and _time_bounds. Works by passing
        millisecond timestamps (expects seconds and the min/max timestamps).
        '''
        self.assertEqual(merge_fit._elapsed_seconds(1000, 3500), 2.5)

        df = make_records([5000, 1000, 3000])
        self.assertEqual(merge_fit._time_bounds(df), (1000, 5000))

    def test_make_messages(self):
        '''
        Functions to test the _make_* message builders. Works by building each
        message from a ten second, 100 m segment and checking a few key fields.
        '''
        df = make_records([0, 10000], distance=[0.0, 100.0])

        file_id = merge_fit._make_file_id_message(0)
        self.assertEqual(file_id.type, FileType.ACTIVITY)

        event = merge_fit._make_timer_event(EventType.START, 0)
        self.assertEqual(event.event, Event.TIMER)
        self.assertEqual(event.event_type, EventType.START)

        lap = merge_fit._make_lap_message(df, 0, Sport.CYCLING, SubSport.GENERIC)
        self.assertAlmostEqual(lap.total_elapsed_time, 10.0, places=2)
        self.assertAlmostEqual(lap.total_distance, 100.0, places=1)

        session = merge_fit._make_session_message(df, 2, 8.0, Sport.CYCLING, SubSport.GENERIC)
        self.assertEqual(session.num_laps, 2)
        self.assertAlmostEqual(session.total_timer_time, 8.0, places=2)
        self.assertAlmostEqual(session.total_distance, 100.0, places=1)

        activity = merge_fit._make_activity_message(10000, 8.0)
        self.assertEqual(activity.num_sessions, 1)
        self.assertAlmostEqual(activity.total_timer_time, 8.0, places=2)

    def test_merge_fit_files(self):
        '''
        Function to test merge_fit_files. Works by building two small FIT files,
        merging them and reading the result back (expects all records and the
        sport), then passing no files (expects ValueError).
        '''
        def write_fit(path, start_ms):
            df = make_records([start_ms + i * 1000 for i in range(3)],
                              heart_rate=[140, 150, 160], source=0,
                              distance=[0.0, 0.0, 0.0])
            merge_fit._build_fit_file(df, Sport.CYCLING, SubSport.GENERIC).to_file(str(path))

        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / 'first.fit'
            second = Path(folder) / 'second.fit'
            output = Path(folder) / 'merged.fit'
            write_fit(first, 1_700_000_000_000)
            write_fit(second, 1_700_000_100_000)

            merge_fit.merge_fit_files([first, second], output)
            decoded = merge_fit.decode_fit_records(output)

        self.assertEqual(len(decoded.records), 6)
        self.assertEqual(decoded.sport, Sport.CYCLING)

        with self.assertRaises(ValueError):
            merge_fit.merge_fit_files([], 'out.fit')


if __name__ == '__main__':
    unittest.main()