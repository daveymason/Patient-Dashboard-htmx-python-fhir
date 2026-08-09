"""Turn a patient's FHIR record into a text block a model can read.

Three properties matter more than the formatting:

1. **Every fact carries its source.** Each line ends with `[ResourceType/id]`,
   so any claim the model makes can be traced back to a resource on the server
   and checked. An assistant that cannot be checked cannot be trusted with a
   clinical question, and a citation the reader cannot resolve is decoration.

2. **The block is deterministic.** The same record renders byte-for-byte the
   same on every turn of a conversation, so provider-side prompt caching hits
   (cached input is ten times cheaper than fresh input). Nothing here may
   depend on wall-clock time, dict iteration order, or set ordering.

3. **The whole record goes by default.** A dense record measures ~19K tokens
   against a 1,050,000-token window, so truncating to "the important bits"
   would drop clinically relevant history to save nothing. Curation happens
   only above a configured ceiling, and allergies are never dropped.

Nothing in this module talks to a model provider -- it only assembles text.
Whether that text may leave the building at all is decided by
`config.ai_blocked_reason`.
"""
import logging
import math
from dataclasses import dataclass, field

from config import deidentify_enabled
from fhir_utils import (
    calculate_age, condition_onset, first_coding_display, format_address,
    format_fhir_date, observation_value, patient_display_name, sort_key,
)

logger = logging.getLogger(__name__)

# Page size for record reads, and how many pages to follow before giving up.
# A patient with more than 2,000 resources is an outlier whose context gets
# curated anyway, so following further pages buys nothing but latency.
_PAGE_COUNT = 200
_MAX_PAGES = 10

# Deliberately pessimistic: ordinary English prose runs about 4 characters per
# token, but a clinical context block is dense with codes, dates, units and
# resource ids, which tokenize worse. Over-estimating means curation triggers
# slightly early -- under-estimating would mean sailing past the repricing
# cliff, so the error is pointed in the safe direction.
_CHARS_PER_TOKEN = 3.5

# Age is generalised above this, following the usual safe-harbour convention:
# a specific age over 89 is identifying on its own in a small population.
_AGE_GENERALISATION_THRESHOLD = 89

NOT_RECORDED = 'not recorded'


def estimate_tokens(text):
    """Approximate token count for a string. See `_CHARS_PER_TOKEN`."""
    return math.ceil(len(text or '') / _CHARS_PER_TOKEN)


@dataclass
class Section:
    """One headed block of the context -- allergies, conditions, and so on.

    `priority` orders curation: when the block exceeds the token ceiling, lines
    are dropped from the lowest-priority section first, oldest first. Allergies
    carry the highest priority and are additionally exempt by name, because a
    summary that silently omits an allergy is worse than no summary.
    """

    key: str
    title: str
    priority: int
    lines: list = field(default_factory=list)
    omitted: int = 0

    @property
    def trimmable(self):
        return self.key != 'allergies'


# Rendering order is fixed, so the block is deterministic and the most
# safety-critical material appears first -- models weight the top of a long
# context more heavily, and allergies are what must not be missed.
_SECTION_ORDER = (
    ('allergies', 'Allergies and intolerances', 100),
    ('conditions', 'Conditions / problem list', 90),
    ('medications', 'Medications', 80),
    ('vitals', 'Vital signs', 60),
    ('labs', 'Laboratory results', 50),
    ('immunizations', 'Immunisations', 40),
    ('procedures', 'Procedures', 30),
    ('encounters', 'Encounters', 20),
)

# Sections reachable through `build_section_context`, mapped to the search that
# fetches them. Each is (resource type, extra search params).
SECTION_SEARCHES = {
    'allergies': ('AllergyIntolerance', {}),
    'conditions': ('Condition', {}),
    'medications': ('MedicationRequest', {}),
    'vitals': ('Observation', {'category': 'vital-signs'}),
    'labs': ('Observation', {'category': 'laboratory'}),
    'immunizations': ('Immunization', {}),
    'procedures': ('Procedure', {}),
    'encounters': ('Encounter', {}),
}


@dataclass
class RecordFetch:
    """What came back from the record server, including what did not.

    `unavailable` and `partial` exist because "the server holds none" and "we
    could not read them" are different facts. Collapsing them is the failure
    mode this codebase is built to avoid, and it is worse here than on screen:
    a clinician seeing an empty panel knows something may be missing, but a
    model told a section is empty will state the patient has no allergies.
    """

    resources: list = field(default_factory=list)
    source: str = ''
    error: str = ''
    unavailable: set = field(default_factory=set)
    partial: bool = False


@dataclass
class PatientContext:
    """An assembled context block, plus what it cost and what it left out."""

    patient_id: str
    ok: bool = False
    error: str = ''
    text: str = ''
    sections: list = field(default_factory=list)
    omitted: list = field(default_factory=list)
    unavailable: set = field(default_factory=set)
    partial: bool = False
    estimated_tokens: int = 0
    deidentified: bool = False
    source: str = ''

    @property
    def is_empty(self):
        """True when the patient exists but has no clinical record to speak of."""
        return self.ok and not any(section.lines for section in self.sections)

    @property
    def complete(self):
        """True when the whole record was readable -- nothing failed or was cut."""
        return self.ok and not self.unavailable and not self.partial


def context_options(config):
    """Read the context settings out of a Flask config.

    Kept here so route code does not re-derive the de-identification rule and
    quietly get it wrong on one path.
    """
    return {
        'deidentify': deidentify_enabled(
            config.get('AI_DEIDENTIFY', 'auto'), config.get('FHIR_SERVER_URL', ''),
        ),
        'token_ceiling': config.get('AI_CONTEXT_TOKEN_CEILING', 120_000),
    }


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _group_by_type(resources):
    grouped = {}
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        resource_type = resource.get('resourceType')
        if not resource_type:
            continue
        grouped.setdefault(resource_type, []).append(resource)
    return grouped


def _dedupe(resources):
    """Drop repeats by (type, id), keeping first occurrence.

    `$everything` paging can hand back the same resource twice, and a record
    that lists the same allergy three times reads as three allergies.
    """
    seen = set()
    unique = []
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        identity = (resource.get('resourceType'), resource.get('id'))
        if identity in seen and identity[1] is not None:
            continue
        seen.add(identity)
        unique.append(resource)
    return unique


def _next_link(result):
    for link in result.data.get('link') or []:
        if isinstance(link, dict) and link.get('relation') == 'next' and link.get('url'):
            return link['url']
    return None


def fetch_record(fhir, patient_id):
    """Pull a patient's whole record. Returns a `RecordFetch`.

    `$everything` is one round trip for the entire record and is what the demo
    server supports, but it is an optional operation -- plenty of servers do not
    implement it, and some restrict it. So a failure falls back to one search
    per resource type, run concurrently, which every FHIR server supports.
    """
    result = fhir.get(f'Patient/{patient_id}/$everything', params={'_count': _PAGE_COUNT})

    if result.ok:
        resources = list(result.entries)
        partial = False
        pages = 1
        while pages < _MAX_PAGES:
            url = _next_link(result)
            if not url:
                break
            result = fhir.get_absolute(url)
            if not result.ok:
                # A partial record is more useful than none, but the caller has
                # to know it is partial. Which resources are missing cannot be
                # known here, so the context says the record is incomplete
                # rather than implying it is whole.
                logger.warning('Stopped paging $everything early: %s', result.error)
                partial = True
                break
            resources.extend(result.entries)
            pages += 1
        else:
            # Ran out of allowed pages with more still on offer.
            partial = bool(_next_link(result))

        return RecordFetch(resources=_dedupe(resources), source='everything',
                           partial=partial)

    if result.status_code == 404:
        return RecordFetch(source='everything',
                           error='That patient could not be found.')

    patient_result = fhir.get(f'Patient/{patient_id}')
    if not patient_result.ok:
        return RecordFetch(
            source='search',
            error=(patient_result.error
                   or 'Could not read this patient from the record server.'),
        )

    specs = {
        key: (resource_type, {'patient': patient_id, '_count': _PAGE_COUNT, **params})
        for key, (resource_type, params) in SECTION_SEARCHES.items()
    }
    results = fhir.get_many(specs)

    resources = [patient_result.data]
    unavailable = set()
    for key, section_result in results.items():
        if section_result.ok:
            resources.extend(section_result.entries)
        else:
            unavailable.add(key)

    if unavailable:
        logger.warning('Record sections unavailable for context: %s',
                       ', '.join(sorted(unavailable)))

    return RecordFetch(resources=_dedupe(resources), source='search',
                       unavailable=unavailable)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _tag(resource):
    return f"[{resource.get('resourceType', 'Resource')}/{resource.get('id', 'unknown')}]"


def _line(parts, resource):
    """One fact, with its source, as a single deterministic line."""
    body = ' | '.join(str(part) for part in parts if part not in (None, ''))
    return f'- {body} {_tag(resource)}'


def _effective_date(resource):
    for key in ('effectiveDateTime', 'authoredOn', 'occurrenceDateTime',
                'performedDateTime', 'recordedDate', 'onsetDateTime', 'date'):
        if resource.get(key):
            return resource[key]
    for key in ('effectivePeriod', 'performedPeriod', 'period'):
        value = resource.get(key)
        if isinstance(value, dict) and value.get('start'):
            return value['start']
    return None


def _sorted_newest_first(resources):
    """Newest first, with the resource id as a tie-break.

    The tie-break is not cosmetic: without it, two same-dated resources can swap
    places between calls and the context block stops being byte-identical,
    which silently costs a prompt-cache hit on every turn.
    """
    return sorted(
        resources,
        key=lambda resource: (sort_key(_effective_date(resource)),
                              str(resource.get('id', ''))),
        reverse=True,
    )


def _render_patient(resource, deidentify):
    """The demographic header. This is the block de-identification acts on."""
    lines = []
    tag = _tag(resource)

    if deidentify:
        lines.append(f'- Name and contact details withheld {tag}')
    else:
        lines.append(f"- Name: {patient_display_name(resource)} {tag}")

    gender = resource.get('gender') or NOT_RECORDED
    lines.append(f'- Gender: {gender}')

    birth_date = resource.get('birthDate')
    age = calculate_age(birth_date)
    if deidentify:
        if age is not None and age > _AGE_GENERALISATION_THRESHOLD:
            lines.append(f'- Age: over {_AGE_GENERALISATION_THRESHOLD}')
        else:
            year = (birth_date or '')[:4]
            lines.append(f"- Year of birth: {year or NOT_RECORDED}"
                         + (f' | Age: {age}' if age is not None else ''))
    else:
        lines.append(f"- Date of birth: {birth_date or NOT_RECORDED}"
                     + (f' | Age: {age}' if age is not None else ''))

    if not deidentify:
        addresses = resource.get('address') or []
        address = format_address(addresses[0]) if addresses else ''
        if address:
            lines.append(f'- Address: {address}')
        contacts = [
            entry['value'] for entry in resource.get('telecom') or []
            if isinstance(entry, dict) and entry.get('value')
        ]
        if contacts:
            lines.append(f"- Contact: {', '.join(contacts)}")
        identifiers = [
            entry['value'] for entry in resource.get('identifier') or []
            if isinstance(entry, dict) and entry.get('value')
        ]
        if identifiers:
            lines.append(f"- Identifiers: {', '.join(identifiers)}")

    marital = first_coding_display(resource.get('maritalStatus', {}))
    if marital:
        lines.append(f'- Marital status: {marital}')

    languages = [
        first_coding_display(entry.get('language', {}))
        for entry in resource.get('communication') or []
        if isinstance(entry, dict) and first_coding_display(entry.get('language', {}))
    ]
    if languages:
        lines.append(f"- Languages: {', '.join(languages)}")

    if resource.get('deceasedBoolean') or resource.get('deceasedDateTime'):
        died = resource.get('deceasedDateTime')
        lines.append(f"- Deceased: {format_fhir_date(died) if died else 'yes'}")

    return lines


def _render_allergy(resource):
    reactions = []
    for reaction in resource.get('reaction') or []:
        if not isinstance(reaction, dict):
            continue
        for manifestation in reaction.get('manifestation') or []:
            label = first_coding_display(manifestation)
            if label:
                reactions.append(label)
    return _line([
        first_coding_display(resource.get('code', {}), fallback='Unknown substance'),
        f"status: {first_coding_display(resource.get('clinicalStatus', {}), fallback='unknown')}",
        f"criticality: {resource.get('criticality') or 'unknown'}",
        f"type: {resource.get('type')}" if resource.get('type') else '',
        f"reaction: {', '.join(reactions)}" if reactions else '',
        f"recorded: {format_fhir_date(resource.get('recordedDate'), fallback='')}"
        if resource.get('recordedDate') else '',
    ], resource)


def _render_condition(resource):
    onset_display, _ = condition_onset(resource)
    onset = onset_display or format_fhir_date(resource.get('recordedDate'), fallback='')
    return _line([
        first_coding_display(resource.get('code', {}), fallback='Unknown condition'),
        f"onset: {onset}" if onset else '',
        f"status: {first_coding_display(resource.get('clinicalStatus', {}), fallback='unknown')}",
        f"verification: {first_coding_display(resource.get('verificationStatus', {}))}"
        if resource.get('verificationStatus') else '',
    ], resource)


def _medication_label(resource):
    concept = resource.get('medicationCodeableConcept')
    if isinstance(concept, dict):
        label = first_coding_display(concept)
        if label:
            return label
    reference = resource.get('medicationReference')
    if isinstance(reference, dict):
        return reference.get('display') or reference.get('reference') or 'Unknown medication'
    return 'Unknown medication'


def _render_medication(resource):
    dosages = [
        dosage.get('text') for dosage in resource.get('dosageInstruction') or []
        if isinstance(dosage, dict) and dosage.get('text')
    ]
    authored = format_fhir_date(resource.get('authoredOn'), fallback='')
    return _line([
        _medication_label(resource),
        f"status: {resource.get('status') or 'unknown'}",
        f"intent: {resource.get('intent')}" if resource.get('intent') else '',
        f"dose: {dosages[0]}" if dosages else '',
        f"authored: {authored}" if authored else '',
    ], resource)


def _render_observation(resource):
    value, unit = observation_value(resource)
    rendered = f'{value} {unit}'.strip() if value != '' else NOT_RECORDED
    interpretation = ''
    for concept in resource.get('interpretation') or []:
        interpretation = first_coding_display(concept)
        if interpretation:
            break
    ranges = []
    for entry in resource.get('referenceRange') or []:
        if not isinstance(entry, dict):
            continue
        low = (entry.get('low') or {}).get('value')
        high = (entry.get('high') or {}).get('value')
        if low is not None or high is not None:
            ranges.append(f"{low if low is not None else ''}-{high if high is not None else ''}")
    date = format_fhir_date(_effective_date(resource), fallback='')
    return _line([
        date or 'undated',
        first_coding_display(resource.get('code', {}), fallback='Unknown observation'),
        rendered,
        f'interpretation: {interpretation}' if interpretation else '',
        f'reference range: {ranges[0]}' if ranges else '',
        f"status: {resource.get('status')}" if resource.get('status') else '',
    ], resource)


def _render_immunization(resource):
    date = format_fhir_date(_effective_date(resource), fallback='')
    return _line([
        date or 'undated',
        first_coding_display(resource.get('vaccineCode', {}), fallback='Unknown vaccine'),
        f"status: {resource.get('status') or 'unknown'}",
    ], resource)


def _render_procedure(resource):
    date = format_fhir_date(_effective_date(resource), fallback='')
    return _line([
        date or 'undated',
        first_coding_display(resource.get('code', {}), fallback='Unknown procedure'),
        f"status: {resource.get('status') or 'unknown'}",
    ], resource)


def _render_encounter(resource):
    period = resource.get('period') if isinstance(resource.get('period'), dict) else {}
    start = format_fhir_date(period.get('start'), fallback='')
    end = format_fhir_date(period.get('end'), fallback='')
    types = [
        first_coding_display(concept)
        for concept in resource.get('type') or []
        if first_coding_display(concept)
    ]
    reasons = [
        first_coding_display(concept)
        for concept in resource.get('reasonCode') or []
        if first_coding_display(concept)
    ]
    return _line([
        f'{start} to {end}' if start and end else (start or 'undated'),
        types[0] if types else (resource.get('class') or {}).get('code', 'Encounter'),
        f"status: {resource.get('status')}" if resource.get('status') else '',
        f"reason: {', '.join(reasons)}" if reasons else '',
    ], resource)


def _observation_category(resource):
    """'vitals', 'labs', or None -- which section an Observation belongs in."""
    for concept in resource.get('category') or []:
        if not isinstance(concept, dict):
            continue
        for coding in concept.get('coding') or []:
            if not isinstance(coding, dict):
                continue
            code = (coding.get('code') or '').lower()
            if code == 'vital-signs':
                return 'vitals'
            if code in ('laboratory', 'lab'):
                return 'labs'
    return None


def _build_sections(resources):
    """Group resources into rendered, ordered sections.

    Takes no de-identification flag because the clinical sections carry no
    direct identifiers to strip -- names, addresses, contact details and record
    numbers all live on the Patient resource, which `_render_patient` handles.
    Free-text `note` elements are deliberately not rendered anywhere: they are
    the one place a clinician's prose can name a person, and there is no
    reliable way to strip that.
    """
    grouped = _group_by_type(resources)
    sections = {
        key: Section(key=key, title=title, priority=priority)
        for key, title, priority in _SECTION_ORDER
    }

    renderers = (
        ('allergies', 'AllergyIntolerance', _render_allergy),
        ('conditions', 'Condition', _render_condition),
        ('medications', 'MedicationRequest', _render_medication),
        ('medications', 'MedicationStatement', _render_medication),
        ('immunizations', 'Immunization', _render_immunization),
        ('procedures', 'Procedure', _render_procedure),
        ('encounters', 'Encounter', _render_encounter),
    )
    for key, resource_type, render in renderers:
        for resource in _sorted_newest_first(grouped.get(resource_type, [])):
            sections[key].lines.append(render(resource))

    # Observations split by category rather than by resource type. An
    # uncategorised one lands in labs: a stray potassium result read as a vital
    # sign is a smaller problem than one that vanishes.
    for resource in _sorted_newest_first(grouped.get('Observation', [])):
        key = _observation_category(resource) or 'labs'
        sections[key].lines.append(_render_observation(resource))

    return [sections[key] for key, _title, _priority in _SECTION_ORDER]


def _omission_notes(sections):
    """What was dropped, in the reader's words."""
    return [
        f'{section.omitted} older {section.title.lower()} '
        f"{'entry' if section.omitted == 1 else 'entries'}"
        for section in sections if section.omitted
    ]


def _trim(sections, target_tokens):
    """Drop at least `target_tokens` worth of the least important lines.

    Lowest-priority section first and, within a section, oldest first -- lines
    are already newest-first, so this pops from the end. Allergies are exempt:
    an assistant that omits an allergy to save tokens is a patient-safety
    defect, and the tokens saved are trivial next to a million-token window.

    Returns how many tokens were actually freed, which is zero when there is
    nothing left that may be dropped.
    """
    freed = 0
    trimmable = sorted(
        (section for section in sections if section.trimmable),
        key=lambda section: section.priority,
    )
    for section in trimmable:
        while section.lines and freed < target_tokens:
            freed += estimate_tokens(section.lines.pop())
            section.omitted += 1
        if freed >= target_tokens:
            break
    return freed


# Each pass trims at least the measured overshoot, so two normally suffice; the
# cap stops a pathological record from looping.
_MAX_CURATION_PASSES = 4


def _fit_to_ceiling(patient_lines, sections, token_ceiling, deidentify,
                    unavailable=(), partial=False):
    """Render, and curate until the *rendered* block fits. Returns (text, omitted).

    Measuring the rendered text rather than the clinical lines alone is the
    point: section headings, the empty-sections list and the curation notice
    are all real tokens, and budgeting without them overshoots the ceiling by
    whatever the formatting costs -- an error pointing in the unsafe direction,
    since the ceiling exists to stay clear of the repricing cliff.
    """
    def render(omitted):
        return _render(patient_lines, sections, omitted, deidentify,
                       unavailable=unavailable, partial=partial)

    for _pass in range(_MAX_CURATION_PASSES):
        omitted = _omission_notes(sections)
        text = render(omitted)
        overshoot = estimate_tokens(text) - token_ceiling
        if overshoot <= 0:
            return text, omitted
        if _trim(sections, overshoot) == 0:
            break  # only undroppable content left

    omitted = _omission_notes(sections)
    return render(omitted), omitted


def _render(patient_lines, sections, omitted, deidentify,
            unavailable=(), partial=False):
    """Assemble the final block. Deterministic: no clock, no unordered iteration."""
    blocks = ['## Patient', '\n'.join(patient_lines)]

    for section in sections:
        if not section.lines:
            continue
        blocks.append(f'## {section.title} ({len(section.lines)})')
        blocks.append('\n'.join(section.lines))

    # "The record holds none" and "we could not read it" are different claims,
    # and only the first is safe to make. A section whose read failed must
    # never appear in the empty list: a model told that allergies are empty
    # will say the patient has no allergies.
    empty = [
        section.title for section in sections
        if not section.lines and section.key not in unavailable
    ]
    unreadable = [
        section.title for section in sections if section.key in unavailable
    ]

    if empty:
        blocks.append('## Sections with no entries on the record')
        blocks.append('\n'.join(f'- {title}' for title in empty))

    if unreadable:
        blocks.append('## Sections that could NOT be read')
        blocks.append('\n'.join(f'- {title}' for title in unreadable))

    notes = []
    if unreadable:
        notes.append(
            'The sections listed as unreadable failed to load from the record '
            'server. Their absence here means the read failed, not that the '
            'record holds nothing. Never state that the patient has none of '
            'these -- say the section could not be read.'
        )
    if partial:
        notes.append(
            'This record was too large to read in full, so it may be missing '
            'entries in any section. Treat it as incomplete.'
        )
    if omitted:
        notes.append(
            'This context was curated to fit a size limit. Omitted: '
            + '; '.join(omitted)
            + '. Say so if a question depends on what was omitted.'
        )
    if deidentify:
        notes.append(
            'Direct identifiers (name, address, contact details, record '
            'numbers) have been withheld from this context.'
        )
    if notes:
        blocks.append('## Notes on this context')
        blocks.append('\n'.join(f'- {note}' for note in notes))

    return '\n\n'.join(blocks) + '\n'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_patient_context(fhir, patient_id, deidentify=False, token_ceiling=120_000):
    """Assemble the full record for `patient_id` into a context block."""
    fetched = fetch_record(fhir, patient_id)
    if fetched.error:
        return PatientContext(patient_id=patient_id, ok=False,
                              error=fetched.error, source=fetched.source)

    patient = next(
        (resource for resource in fetched.resources
         if resource.get('resourceType') == 'Patient'), None,
    )
    if patient is None:
        return PatientContext(
            patient_id=patient_id, ok=False, source=fetched.source,
            error='The record server returned no patient for that id.',
        )

    patient_lines = _render_patient(patient, deidentify)
    sections = _build_sections(
        [resource for resource in fetched.resources if resource is not patient]
    )
    text, omitted = _fit_to_ceiling(
        patient_lines, sections, token_ceiling, deidentify,
        unavailable=fetched.unavailable, partial=fetched.partial,
    )

    return PatientContext(
        patient_id=patient_id,
        ok=True,
        text=text,
        sections=sections,
        omitted=omitted,
        unavailable=fetched.unavailable,
        partial=fetched.partial,
        estimated_tokens=estimate_tokens(text),
        deidentified=deidentify,
        source=fetched.source,
    )


def build_section_context(fhir, patient_id, section, deidentify=False,
                          token_ceiling=120_000):
    """Assemble one section of the record -- for the narrow, single-panel buttons.

    Cheaper and sharper than the full record when the question is only about
    one panel: an allergy cross-check does not need forty encounters in front
    of it. Raises ValueError on an unknown section rather than silently
    returning an empty context, which would read to a caller as "the patient
    has none of those".
    """
    if section not in SECTION_SEARCHES:
        raise ValueError(f'Unknown context section: {section!r}')

    resource_type, params = SECTION_SEARCHES[section]
    results = fhir.get_many({
        'patient': (f'Patient/{patient_id}', None),
        'section': (resource_type, {'patient': patient_id, '_count': _PAGE_COUNT, **params}),
    })

    patient_result = results['patient']
    if not patient_result.ok:
        return PatientContext(
            patient_id=patient_id, ok=False, source='search',
            error=patient_result.error or 'Could not read this patient.',
        )

    section_result = results['section']
    if not section_result.ok:
        return PatientContext(
            patient_id=patient_id, ok=False, source='search',
            error=section_result.error or f'Could not read {section} for this patient.',
        )

    patient_lines = _render_patient(patient_result.data, deidentify)
    all_sections = _build_sections(section_result.entries)
    sections = [entry for entry in all_sections if entry.key == section]
    text, omitted = _fit_to_ceiling(patient_lines, sections, token_ceiling, deidentify)

    return PatientContext(
        patient_id=patient_id,
        ok=True,
        text=text,
        sections=sections,
        omitted=omitted,
        estimated_tokens=estimate_tokens(text),
        deidentified=deidentify,
        source='search',
    )
