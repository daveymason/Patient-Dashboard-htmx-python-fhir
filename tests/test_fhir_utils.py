"""Unit tests for the parsing helpers.

The date cases are the exact strings pulled from the live HAPI server that
crashed the previous `effectiveDateTime[:-6]` implementation.
"""
from datetime import datetime

import pytest

from fhir_utils import (
    calculate_age, first_coding_display, format_address, format_fhir_date,
    observation_code, observation_value, parse_fhir_datetime,
    patient_display_name, patient_initials, sort_key,
)


class TestParseFhirDatetime:
    @pytest.mark.parametrize('value', [
        '2026-08-07T16:49:48.938Z',      # millis + Z
        '2026-08-07T11:39:49Z',          # no millis + Z   -> used to crash
        '2026-08-07T07:44:07.470+00:00',  # millis + offset -> used to crash
        '2026-08-07T20:30:00+08:00',     # offset, no millis
        '2026-08-07',                    # date only
        '2026-08',                       # partial: year-month
        '2026',                          # partial: year
    ])
    def test_parses_every_shape_the_server_returns(self, value):
        assert parse_fhir_datetime(value) is not None

    @pytest.mark.parametrize('value', [None, '', '   ', 'not-a-date', 42, {}, []])
    def test_returns_none_rather_than_raising(self, value):
        assert parse_fhir_datetime(value) is None

    def test_formats_all_live_samples_to_the_same_day(self):
        for value in ('2026-08-07T16:49:48.938Z', '2026-08-07T11:39:49Z',
                      '2026-08-07T07:44:07.470+00:00'):
            assert format_fhir_date(value) == '2026-08-07'

    def test_seconds_are_not_silently_truncated(self):
        # The old slicing turned ...:48.938Z into ...:4, quietly changing the
        # seconds value. Verify the whole time survives.
        parsed = parse_fhir_datetime('2026-08-07T16:49:48.938Z')
        assert (parsed.hour, parsed.minute, parsed.second) == (16, 49, 48)

    def test_unparseable_date_falls_back(self):
        assert format_fhir_date('rubbish') == 'Unknown'
        assert format_fhir_date(None, fallback='n/a') == 'n/a'


class TestSortKey:
    def test_undated_entries_sort_oldest_not_newest(self):
        rows = [
            {'d': '2026-01-01T00:00:00Z'},
            {'d': None},
            {'d': '2026-06-01T00:00:00Z'},
        ]
        rows.sort(key=lambda r: sort_key(r['d']), reverse=True)
        assert [r['d'] for r in rows][0].startswith('2026-06')
        assert rows[-1]['d'] is None

    def test_mixing_aware_and_naive_does_not_raise(self):
        # Comparing an offset-aware and a naive datetime raises TypeError;
        # sort_key must flatten both to the same kind.
        values = ['2026-08-07T10:00:00+02:00', '2026-08-07', '2026-08-07T09:00:00Z']
        assert sorted(values, key=sort_key)

    def test_returns_datetime_min_for_missing(self):
        assert sort_key(None) == datetime.min


class TestPatientName:
    def test_prefers_text(self):
        assert patient_display_name(
            {'name': [{'text': 'Synthetic Patient SYN-2', 'family': 'SYN-2'}]}
        ) == 'Synthetic Patient SYN-2'

    def test_builds_from_given_and_family(self):
        assert patient_display_name(
            {'name': [{'given': ['Ada', 'M'], 'family': 'Lovelace'}]}
        ) == 'Ada M Lovelace'

    @pytest.mark.parametrize('resource', [
        {},                                  # no name at all -> used to crash
        {'name': []},
        {'name': [{}]},
        {'name': [{'given': []}]},
        {'name': [None]},
    ])
    def test_never_raises_on_missing_name(self, resource):
        assert patient_display_name(resource) == 'Unknown'
        assert patient_initials(resource) in ('?', 'U')

    def test_initials(self):
        assert patient_initials({'name': [{'given': ['Ada'], 'family': 'Lovelace'}]}) == 'AL'
        assert patient_initials({'name': [{'given': ['Cher']}]}) == 'C'
        assert patient_initials({}) == '?'


class TestObservationValue:
    def test_quantity(self):
        assert observation_value(
            {'valueQuantity': {'value': 5.4, 'unit': 'mmol/L'}}
        ) == (5.4, 'mmol/L')

    def test_codeable_concept(self):
        value, _unit = observation_value(
            {'valueCodeableConcept': {'coding': [{'display': 'Positive'}]}}
        )
        assert value == 'Positive'

    def test_string(self):
        assert observation_value({'valueString': 'See report'})[0] == 'See report'

    def test_boolean(self):
        assert observation_value({'valueBoolean': True})[0] == 'Yes'

    def test_blood_pressure_components(self):
        # No top-level value at all -- this is what raised UndefinedError in
        # the template and 500'd the Lab Results panel.
        resource = {
            'component': [
                {'code': {'coding': [{'display': 'Systolic blood pressure'}]},
                 'valueQuantity': {'value': 120, 'unit': 'mmHg'}},
                {'code': {'coding': [{'display': 'Diastolic blood pressure'}]},
                 'valueQuantity': {'value': 80, 'unit': 'mmHg'}},
            ]
        }
        value, unit = observation_value(resource)
        assert '120' in str(value) and '80' in str(value)
        assert unit == 'mmHg'

    def test_no_value_returns_empty_not_exception(self):
        assert observation_value({}) == ('', '')
        assert observation_value({'status': 'final'}) == ('', '')

    def test_data_absent_reason(self):
        value, _ = observation_value(
            {'dataAbsentReason': {'coding': [{'display': 'Not performed'}]}}
        )
        assert value == 'Not performed'


class TestMisc:
    def test_first_coding_display_prefers_text(self):
        assert first_coding_display({'text': 'Penicillin'}) == 'Penicillin'
        assert first_coding_display({'coding': [{'display': 'Penicillin'}]}) == 'Penicillin'
        assert first_coding_display({'coding': [{'code': 'PCN'}]}) == 'PCN'
        assert first_coding_display({}) == ''
        assert first_coding_display(None, fallback='x') == 'x'

    def test_observation_code(self):
        assert observation_code({'code': {'text': 'Glucose'}}) == 'Glucose'
        assert observation_code({}) == 'Unknown'

    def test_format_address_does_not_mutate_the_source(self):
        address = {'line': ['1 Main St'], 'city': 'Dublin', 'postalCode': 'D01'}
        original_lines = list(address['line'])
        formatted = format_address(address)
        assert formatted == '1 Main St, Dublin, D01'
        # The old code extended the list returned by .get(), corrupting the
        # caller's resource.
        assert address['line'] == original_lines

    def test_format_address_handles_missing(self):
        assert format_address({}) == ''
        assert format_address(None) == ''

    def test_calculate_age(self):
        this_year = datetime.now().year
        assert calculate_age(f'{this_year - 30}-01-01') in (29, 30)
        assert calculate_age(None) is None
        assert calculate_age('rubbish') is None
