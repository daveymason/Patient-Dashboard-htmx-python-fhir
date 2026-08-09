"""Tests for patient context assembly.

Nothing here touches the network or a model provider: the FHIR client is a
fake that answers from dictionaries.
"""
import pytest

from ai.context import (
    PatientContext, build_patient_context, build_section_context,
    context_options, estimate_tokens, fetch_record,
)
from config import TestConfig
from fhir_client import FhirResult

PATIENT_ID = 'p1'


# ---------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------

def bundle(*resources, next_url=None):
    payload = {
        'resourceType': 'Bundle',
        'type': 'searchset',
        'entry': [{'resource': resource} for resource in resources],
    }
    if next_url:
        payload['link'] = [{'relation': 'next', 'url': next_url}]
    return payload


class FakeFhir:
    """Answers whatever the test put in `responses`, keyed by path.

    Anything not listed comes back as an empty successful search, which is what
    a real server does for a patient with no resources of that type -- the
    distinction the whole codebase turns on.
    """

    def __init__(self, responses=None, absolute=None):
        self.responses = responses or {}
        self.absolute = absolute or {}
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path in self.responses:
            return self.responses[path]
        return FhirResult(True, data=bundle())

    def get_absolute(self, url):
        self.calls.append((url, None))
        if url in self.absolute:
            return self.absolute[url]
        return FhirResult(False, error='Invalid pagination link.')

    def get_many(self, spec):
        return {key: self.get(path, params) for key, (path, params) in spec.items()}


PATIENT = {
    'resourceType': 'Patient',
    'id': PATIENT_ID,
    'name': [{'text': 'Ada Lovelace'}],
    'gender': 'female',
    'birthDate': '1975-12-10',
    'address': [{'line': ['12 Analytical Way'], 'city': 'London', 'postalCode': 'N1 1AA'}],
    'telecom': [{'system': 'phone', 'value': '020 7946 0000'}],
    'identifier': [{'value': 'NHS-4409876543'}],
}

ALLERGY = {
    'resourceType': 'AllergyIntolerance',
    'id': 'a1',
    'code': {'text': 'Penicillin'},
    'criticality': 'high',
    'clinicalStatus': {'text': 'active'},
    'reaction': [{'manifestation': [{'text': 'Anaphylaxis'}]}],
}

CONDITION = {
    'resourceType': 'Condition',
    'id': 'c1',
    'code': {'text': 'Type 2 diabetes mellitus'},
    'clinicalStatus': {'text': 'active'},
    'onsetDateTime': '2019-04-02',
}

MEDICATION = {
    'resourceType': 'MedicationRequest',
    'id': 'm1',
    'medicationCodeableConcept': {'text': 'Metformin 500mg'},
    'status': 'active',
    'intent': 'order',
    'authoredOn': '2023-06-01',
    'dosageInstruction': [{'text': 'One tablet twice daily'}],
}

VITAL = {
    'resourceType': 'Observation',
    'id': 'o1',
    'category': [{'coding': [{'code': 'vital-signs'}]}],
    'code': {'text': 'Heart rate'},
    'valueQuantity': {'value': 72, 'unit': 'bpm'},
    'effectiveDateTime': '2024-02-01T09:00:00Z',
}

LAB = {
    'resourceType': 'Observation',
    'id': 'o2',
    'category': [{'coding': [{'code': 'laboratory'}]}],
    'code': {'text': 'HbA1c'},
    'valueQuantity': {'value': 58, 'unit': 'mmol/mol'},
    'effectiveDateTime': '2024-01-15',
}

FULL_RECORD = (PATIENT, ALLERGY, CONDITION, MEDICATION, VITAL, LAB)


def everything(*resources, next_url=None):
    return FakeFhir({
        f'Patient/{PATIENT_ID}/$everything':
            FhirResult(True, data=bundle(*resources, next_url=next_url)),
    })


# ---------------------------------------------------------------------------


class TestFetching:
    def test_everything_is_tried_first(self):
        fhir = everything(*FULL_RECORD)
        fetched = fetch_record(fhir, PATIENT_ID)
        assert fetched.source == 'everything'
        assert fetched.error == ''
        assert len(fetched.resources) == len(FULL_RECORD)
        assert fhir.calls[0][0] == f'Patient/{PATIENT_ID}/$everything'

    def test_falls_back_to_per_type_search(self):
        """$everything is an optional operation; plenty of servers refuse it."""
        fhir = FakeFhir({
            f'Patient/{PATIENT_ID}/$everything':
                FhirResult(False, status_code=501, error='Not supported'),
            f'Patient/{PATIENT_ID}': FhirResult(True, data=PATIENT),
            'Condition': FhirResult(True, data=bundle(CONDITION)),
            'AllergyIntolerance': FhirResult(True, data=bundle(ALLERGY)),
        })
        fetched = fetch_record(fhir, PATIENT_ID)
        assert fetched.source == 'search'
        assert fetched.error == ''
        types = {resource['resourceType'] for resource in fetched.resources}
        assert types == {'Patient', 'Condition', 'AllergyIntolerance'}

    def test_missing_patient_is_an_error_not_an_empty_record(self):
        fhir = FakeFhir({
            f'Patient/{PATIENT_ID}/$everything': FhirResult(False, status_code=404),
        })
        context = build_patient_context(fhir, PATIENT_ID)
        assert context.ok is False
        assert 'could not be found' in context.error

    def test_unreachable_server_does_not_render_as_an_empty_record(self):
        fhir = FakeFhir({
            f'Patient/{PATIENT_ID}/$everything': FhirResult(False, error='timeout'),
            f'Patient/{PATIENT_ID}': FhirResult(False, error='Could not reach the record server.'),
        })
        context = build_patient_context(fhir, PATIENT_ID)
        assert context.ok is False
        assert context.text == ''

    def test_pagination_is_followed(self):
        fhir = everything(PATIENT, CONDITION, next_url='http://fhir.test/baseR4?page=2')
        fhir.absolute['http://fhir.test/baseR4?page=2'] = FhirResult(
            True, data=bundle(ALLERGY),
        )
        fetched = fetch_record(fhir, PATIENT_ID)
        assert {resource['id'] for resource in fetched.resources} == {PATIENT_ID, 'c1', 'a1'}
        assert fetched.partial is False

    def test_repeated_resources_across_pages_are_deduped(self):
        """A record listing the same allergy twice reads as two allergies."""
        fhir = everything(PATIENT, ALLERGY, next_url='http://fhir.test/baseR4?page=2')
        fhir.absolute['http://fhir.test/baseR4?page=2'] = FhirResult(
            True, data=bundle(ALLERGY, CONDITION),
        )
        context = build_patient_context(fhir, PATIENT_ID)
        assert context.text.count('[AllergyIntolerance/a1]') == 1

    def test_a_failed_later_page_still_yields_a_usable_context(self):
        fhir = everything(PATIENT, ALLERGY, next_url='http://fhir.test/baseR4?page=2')
        # get_absolute has no entry for that URL, so it fails.
        context = build_patient_context(fhir, PATIENT_ID)
        assert context.ok is True
        assert 'Penicillin' in context.text

    def test_a_partial_record_says_so_rather_than_reading_as_whole(self):
        fhir = everything(PATIENT, ALLERGY, next_url='http://fhir.test/baseR4?page=2')
        context = build_patient_context(fhir, PATIENT_ID)
        assert context.partial is True
        assert context.complete is False
        assert 'Treat it as incomplete' in context.text


class TestFailureIsNotEmptiness:
    """The distinction the whole codebase turns on, and it matters more here
    than on screen: a clinician seeing a blank panel knows something might be
    missing, but a model told a section is empty will state the patient has
    no allergies."""

    def _fhir_with_failed_allergies(self):
        return FakeFhir({
            f'Patient/{PATIENT_ID}/$everything':
                FhirResult(False, status_code=501, error='Not supported'),
            f'Patient/{PATIENT_ID}': FhirResult(True, data=PATIENT),
            'Condition': FhirResult(True, data=bundle(CONDITION)),
            'AllergyIntolerance': FhirResult(False, error='Could not reach the record server.'),
        })

    def test_an_unreadable_section_is_recorded_as_unavailable(self):
        fetched = fetch_record(self._fhir_with_failed_allergies(), PATIENT_ID)
        assert 'allergies' in fetched.unavailable

    def test_an_unreadable_section_is_never_listed_as_empty(self):
        context = build_patient_context(self._fhir_with_failed_allergies(), PATIENT_ID)
        empty_block = context.text.split(
            '## Sections with no entries on the record')[1].split('##')[0]
        assert 'Allergies' not in empty_block

    def test_an_unreadable_section_is_called_out_explicitly(self):
        context = build_patient_context(self._fhir_with_failed_allergies(), PATIENT_ID)
        assert '## Sections that could NOT be read' in context.text
        unreadable = context.text.split(
            '## Sections that could NOT be read')[1].split('##')[0]
        assert 'Allergies and intolerances' in unreadable

    def test_the_model_is_told_not_to_infer_absence(self):
        context = build_patient_context(self._fhir_with_failed_allergies(), PATIENT_ID)
        assert 'Never state that the patient has none of these' in context.text

    def test_readable_sections_are_unaffected(self):
        context = build_patient_context(self._fhir_with_failed_allergies(), PATIENT_ID)
        assert context.ok is True
        assert 'Type 2 diabetes mellitus' in context.text
        assert context.complete is False

    def test_a_fully_readable_record_is_marked_complete(self):
        context = build_patient_context(everything(*FULL_RECORD), PATIENT_ID)
        assert context.complete is True
        assert 'could NOT be read' not in context.text


class TestRendering:
    def test_every_clinical_line_carries_its_source(self):
        context = build_patient_context(everything(*FULL_RECORD), PATIENT_ID)
        clinical = [
            line for line in context.text.splitlines()
            if line.startswith('- ') and 'Gender' not in line and 'birth' not in line
        ]
        tagged = [line for line in clinical if '[' in line and ']' in line]
        assert tagged, 'expected tagged clinical lines'
        for resource in (ALLERGY, CONDITION, MEDICATION, VITAL, LAB):
            tag = f"[{resource['resourceType']}/{resource['id']}]"
            assert tag in context.text

    def test_sections_are_present_and_ordered_with_allergies_first(self):
        context = build_patient_context(everything(*FULL_RECORD), PATIENT_ID)
        text = context.text
        assert text.index('## Allergies') < text.index('## Conditions')
        assert text.index('## Conditions') < text.index('## Medications')

    def test_observations_split_into_vitals_and_labs(self):
        context = build_patient_context(everything(*FULL_RECORD), PATIENT_ID)
        vitals = context.text.split('## Vital signs')[1].split('##')[0]
        labs = context.text.split('## Laboratory results')[1].split('##')[0]
        assert 'Heart rate' in vitals and 'HbA1c' not in vitals
        assert 'HbA1c' in labs and 'Heart rate' not in labs

    def test_uncategorised_observation_lands_in_labs_rather_than_vanishing(self):
        stray = {**LAB, 'id': 'o3', 'category': [], 'code': {'text': 'Potassium'}}
        context = build_patient_context(everything(PATIENT, stray), PATIENT_ID)
        assert 'Potassium' in context.text

    def test_empty_sections_are_stated_not_merely_absent(self):
        """"Not in the context" and "the record holds none" are different
        claims, and only one of them is safe for the model to make."""
        context = build_patient_context(everything(PATIENT, CONDITION), PATIENT_ID)
        assert '## Sections with no entries on the record' in context.text
        assert 'Allergies and intolerances' in context.text.split(
            '## Sections with no entries on the record')[1]

    def test_empty_record_is_ok_and_flagged(self):
        context = build_patient_context(everything(PATIENT), PATIENT_ID)
        assert context.ok is True
        assert context.is_empty is True

    def test_values_and_units_are_rendered(self):
        context = build_patient_context(everything(PATIENT, VITAL), PATIENT_ID)
        assert '72 bpm' in context.text

    def test_dosage_is_rendered(self):
        context = build_patient_context(everything(PATIENT, MEDICATION), PATIENT_ID)
        assert 'One tablet twice daily' in context.text

    def test_defensive_against_junk_resources(self):
        junk = [PATIENT, {'resourceType': 'Condition', 'id': 'c9'}, {}, 'not a dict', None]
        fhir = FakeFhir({
            f'Patient/{PATIENT_ID}/$everything': FhirResult(True, data={
                'resourceType': 'Bundle',
                'entry': [{'resource': resource} for resource in junk],
            }),
        })
        context = build_patient_context(fhir, PATIENT_ID)
        assert context.ok is True
        assert 'Unknown condition' in context.text


class TestDeterminism:
    """Byte-identical output is what makes provider-side prompt caching hit,
    and cached input is a tenth of the price of fresh input."""

    def test_same_record_renders_identically(self):
        first = build_patient_context(everything(*FULL_RECORD), PATIENT_ID).text
        second = build_patient_context(everything(*FULL_RECORD), PATIENT_ID).text
        assert first == second

    def test_same_dated_resources_do_not_swap_places(self):
        same_date = [
            {**CONDITION, 'id': f'c{n}', 'code': {'text': f'Condition {n}'}}
            for n in range(8)
        ]
        forward = build_patient_context(everything(PATIENT, *same_date), PATIENT_ID).text
        reversed_order = build_patient_context(
            everything(PATIENT, *reversed(same_date)), PATIENT_ID,
        ).text
        assert forward == reversed_order


class TestCuration:
    def test_a_normal_record_is_sent_whole(self):
        context = build_patient_context(everything(*FULL_RECORD), PATIENT_ID)
        assert context.omitted == []
        assert context.estimated_tokens < 1000

    def test_allergies_survive_a_ceiling_that_drops_everything_else(self):
        """Omitting an allergy to save tokens is a patient-safety defect, and
        the tokens saved are trivial against a 1.05M-token window."""
        bulk = [
            {**LAB, 'id': f'lab{n}', 'code': {'text': f'Result {n}'}}
            for n in range(200)
        ]
        context = build_patient_context(
            everything(PATIENT, ALLERGY, *bulk), PATIENT_ID, token_ceiling=200,
        )
        assert 'Penicillin' in context.text
        assert '[AllergyIntolerance/a1]' in context.text
        assert context.omitted

    def test_lowest_priority_section_is_trimmed_first(self):
        encounters = [
            {'resourceType': 'Encounter', 'id': f'e{n}', 'status': 'finished',
             'period': {'start': f'20{n:02d}-01-01', 'end': f'20{n:02d}-01-02'}}
            for n in range(40)
        ]
        context = build_patient_context(
            everything(PATIENT, CONDITION, *encounters), PATIENT_ID, token_ceiling=200,
        )
        assert 'Type 2 diabetes mellitus' in context.text
        assert any('encounter' in note.lower() for note in context.omitted)

    def test_curation_is_declared_in_the_context_itself(self):
        bulk = [{**LAB, 'id': f'lab{n}'} for n in range(200)]
        context = build_patient_context(
            everything(PATIENT, *bulk), PATIENT_ID, token_ceiling=200,
        )
        assert 'curated to fit a size limit' in context.text
        assert 'Omitted:' in context.text

    # Ceilings above the irreducible floor -- the patient header, the
    # empty-section list and the curation notice cannot be dropped, so a
    # ceiling below their combined size is unsatisfiable by construction and is
    # covered by test_undroppable_content_does_not_loop_forever instead.
    @pytest.mark.parametrize('ceiling', [300, 600, 1200])
    def test_the_rendered_block_actually_fits_the_ceiling(self, ceiling):
        """Regression: budgeting only the clinical lines ignored the section
        headings, the empty-sections list and the curation notice, so the block
        that went out was larger than the ceiling allowed -- an error pointing
        the wrong way, since the ceiling exists to stay clear of the repricing
        cliff."""
        bulk = [
            {**LAB, 'id': f'lab{n}', 'code': {'text': f'Laboratory result {n}'},
             'effectiveDateTime': f'20{n % 20 + 5:02d}-01-01'}
            for n in range(150)
        ]
        encounters = [
            {'resourceType': 'Encounter', 'id': f'e{n}', 'status': 'finished',
             'period': {'start': '2020-01-01', 'end': '2020-01-02'}}
            for n in range(30)
        ]
        context = build_patient_context(
            everything(PATIENT, CONDITION, *bulk, *encounters),
            PATIENT_ID, token_ceiling=ceiling,
        )
        assert context.estimated_tokens <= ceiling
        assert estimate_tokens(context.text) <= ceiling

    def test_omission_notes_read_as_english(self):
        bulk = [{**LAB, 'id': f'lab{n}'} for n in range(30)]
        context = build_patient_context(
            everything(PATIENT, *bulk), PATIENT_ID, token_ceiling=200,
        )
        assert not any('1 older' in note and 'entries' in note
                       for note in context.omitted)

    def test_undroppable_content_does_not_loop_forever(self):
        """A ceiling smaller than the allergies alone must terminate, keeping
        the allergies and saying the block was curated."""
        allergies = [
            {**ALLERGY, 'id': f'a{n}', 'code': {'text': f'Allergen number {n}'}}
            for n in range(40)
        ]
        context = build_patient_context(
            everything(PATIENT, *allergies, CONDITION), PATIENT_ID, token_ceiling=10,
        )
        assert context.ok is True
        assert context.text.count('[AllergyIntolerance/') == 40

    def test_newest_entries_are_the_ones_kept(self):
        old = {**LAB, 'id': 'old', 'effectiveDateTime': '1999-01-01',
               'code': {'text': 'Ancient result'}}
        new = {**LAB, 'id': 'new', 'effectiveDateTime': '2025-01-01',
               'code': {'text': 'Recent result'}}
        filler = [{**LAB, 'id': f'f{n}', 'effectiveDateTime': '2024-01-01'}
                  for n in range(60)]
        context = build_patient_context(
            everything(PATIENT, old, new, *filler), PATIENT_ID, token_ceiling=400,
        )
        assert context.omitted, 'expected a partial trim, not a full one'
        assert 'Recent result' in context.text
        assert 'Ancient result' not in context.text


class TestDeidentification:
    def test_direct_identifiers_are_withheld(self):
        context = build_patient_context(
            everything(*FULL_RECORD), PATIENT_ID, deidentify=True,
        )
        assert context.deidentified is True
        for identifier in ('Ada Lovelace', '12 Analytical Way',
                           '020 7946 0000', 'NHS-4409876543'):
            assert identifier not in context.text

    def test_clinical_content_survives_deidentification(self):
        context = build_patient_context(
            everything(*FULL_RECORD), PATIENT_ID, deidentify=True,
        )
        assert 'Penicillin' in context.text
        assert 'Type 2 diabetes mellitus' in context.text
        assert 'female' in context.text

    def test_exact_date_of_birth_is_reduced_to_a_year(self):
        context = build_patient_context(
            everything(PATIENT), PATIENT_ID, deidentify=True,
        )
        assert '1975-12-10' not in context.text
        assert '1975' in context.text

    def test_ages_over_89_are_generalised(self):
        elder = {**PATIENT, 'birthDate': '1920-01-01'}
        context = build_patient_context(
            everything(elder), PATIENT_ID, deidentify=True,
        )
        assert 'over 89' in context.text
        assert '1920' not in context.text

    def test_identifiers_are_present_when_deidentification_is_off(self):
        context = build_patient_context(everything(PATIENT), PATIENT_ID)
        assert 'Ada Lovelace' in context.text
        assert 'NHS-4409876543' in context.text

    def test_the_context_says_when_it_has_been_reduced(self):
        context = build_patient_context(
            everything(PATIENT), PATIENT_ID, deidentify=True,
        )
        assert 'Direct identifiers' in context.text


class TestContextOptions:
    """The default must be the safe one: forgetting to configure this should
    de-identify, not disclose."""

    def test_demo_server_does_not_deidentify_by_default(self):
        options = context_options({
            'FHIR_SERVER_URL': 'https://hapi.fhir.org/baseR4', 'AI_DEIDENTIFY': 'auto',
        })
        assert options['deidentify'] is False

    def test_unknown_server_deidentifies_by_default(self):
        options = context_options({
            'FHIR_SERVER_URL': 'https://fhir.myhospital.nhs.uk/R4', 'AI_DEIDENTIFY': 'auto',
        })
        assert options['deidentify'] is True

    def test_absent_setting_behaves_as_auto(self):
        assert context_options({'FHIR_SERVER_URL': 'https://x.example/R4'})['deidentify'] is True

    def test_explicit_settings_win_both_ways(self):
        forced_on = context_options({
            'FHIR_SERVER_URL': 'https://hapi.fhir.org/baseR4', 'AI_DEIDENTIFY': 'true',
        })
        forced_off = context_options({
            'FHIR_SERVER_URL': 'https://fhir.myhospital.nhs.uk/R4', 'AI_DEIDENTIFY': 'false',
        })
        assert forced_on['deidentify'] is True
        assert forced_off['deidentify'] is False

    def test_ceiling_stays_well_under_the_repricing_cliff(self):
        from config import AI_INPUT_TOKEN_CLIFF
        assert TestConfig.AI_CONTEXT_TOKEN_CEILING < AI_INPUT_TOKEN_CLIFF


class TestSectionContext:
    def test_only_the_requested_section_is_fetched_and_rendered(self):
        fhir = FakeFhir({
            f'Patient/{PATIENT_ID}': FhirResult(True, data=PATIENT),
            'AllergyIntolerance': FhirResult(True, data=bundle(ALLERGY)),
            'Condition': FhirResult(True, data=bundle(CONDITION)),
        })
        context = build_section_context(fhir, PATIENT_ID, 'allergies')
        assert 'Penicillin' in context.text
        assert 'Type 2 diabetes' not in context.text
        assert [call[0] for call in fhir.calls].count('Condition') == 0

    def test_vitals_and_labs_are_separately_addressable(self):
        fhir = FakeFhir({
            f'Patient/{PATIENT_ID}': FhirResult(True, data=PATIENT),
            'Observation': FhirResult(True, data=bundle(VITAL)),
        })
        context = build_section_context(fhir, PATIENT_ID, 'vitals')
        assert 'Heart rate' in context.text
        vitals_call = next(call for call in fhir.calls if call[0] == 'Observation')
        assert vitals_call[1]['category'] == 'vital-signs'

    def test_unknown_section_raises_rather_than_returning_empty(self):
        """An empty context would read to the caller as "the patient has none
        of those", which is a different and false claim."""
        with pytest.raises(ValueError):
            build_section_context(FakeFhir(), PATIENT_ID, 'horoscope')

    def test_a_failed_section_read_is_an_error_not_an_empty_section(self):
        fhir = FakeFhir({
            f'Patient/{PATIENT_ID}': FhirResult(True, data=PATIENT),
            'AllergyIntolerance': FhirResult(False, error='Could not reach the record server.'),
        })
        context = build_section_context(fhir, PATIENT_ID, 'allergies')
        assert context.ok is False
        assert context.text == ''

    def test_section_context_is_deidentified_when_asked(self):
        fhir = FakeFhir({
            f'Patient/{PATIENT_ID}': FhirResult(True, data=PATIENT),
            'AllergyIntolerance': FhirResult(True, data=bundle(ALLERGY)),
        })
        context = build_section_context(fhir, PATIENT_ID, 'allergies', deidentify=True)
        assert 'Ada Lovelace' not in context.text
        assert 'Penicillin' in context.text


class TestTokenEstimate:
    def test_estimate_is_pessimistic_rather_than_optimistic(self):
        """Erring high means curating slightly early; erring low means sailing
        past the repricing cliff."""
        text = 'a' * 400
        assert estimate_tokens(text) >= 100

    def test_empty_input_costs_nothing(self):
        assert estimate_tokens('') == 0
        assert estimate_tokens(None) == 0

    def test_reported_token_count_tracks_the_rendered_text(self):
        context = build_patient_context(everything(*FULL_RECORD), PATIENT_ID)
        assert context.estimated_tokens == estimate_tokens(context.text)


class TestPatientContextShape:
    def test_a_failed_context_is_falsy_about_being_ok(self):
        context = PatientContext(patient_id='x')
        assert context.ok is False
        assert context.is_empty is False  # not ok, so "empty" says nothing
