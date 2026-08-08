"""Parsing and formatting helpers for FHIR resources.

Every function here is defensive: FHIR marks almost every element as optional,
so anything that indexes into a resource must cope with the element being
absent, empty, or a different shape than expected.
"""
from datetime import datetime, timezone

# FHIR permits partial dates (`YYYY`, `YYYY-MM`) wherever a `date` is allowed.
_PARTIAL_DATE_FORMATS = ('%Y-%m-%d', '%Y-%m', '%Y')

UNKNOWN_DATE = 'Unknown'


def parse_fhir_datetime(value):
    """Parse a FHIR date/dateTime/instant into a datetime, or None.

    Handles the three shapes real servers return -- trailing `Z`, explicit
    offsets, and fractional seconds -- plus partial dates. Returns None rather
    than raising so callers can decide how to present a missing date.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    # `Z` is valid ISO 8601 but datetime.fromisoformat only accepts it from
    # Python 3.11 onward, so normalise it to an explicit UTC offset.
    if text[-1] in ('Z', 'z'):
        text = text[:-1] + '+00:00'

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    for fmt in _PARTIAL_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def format_fhir_date(value, fallback=UNKNOWN_DATE):
    """Render a FHIR date/dateTime as YYYY-MM-DD, or `fallback` if unparseable."""
    parsed = parse_fhir_datetime(value)
    return parsed.strftime('%Y-%m-%d') if parsed else fallback


def sort_key(value):
    """Build a comparable key from a FHIR date, sorting unknowns oldest.

    Mixing offset-aware and naive datetimes raises TypeError when compared, so
    everything is flattened to naive UTC. Undated entries sort to the far past,
    which keeps them at the bottom of a newest-first list instead of the top.
    """
    parsed = parse_fhir_datetime(value) if isinstance(value, str) else value
    if parsed is None:
        return datetime.min
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def patient_display_name(resource, fallback='Unknown'):
    """Best available human name for a Patient resource.

    FHIR's own guidance is to prefer `name.text` when present, since it carries
    the name as the source system chose to render it.
    """
    for entry in resource.get('name') or []:
        if not isinstance(entry, dict):
            continue
        text = entry.get('text')
        if text:
            return text
        parts = [part for part in (entry.get('given') or []) if part]
        family = entry.get('family')
        if family:
            parts.append(family)
        if parts:
            return ' '.join(parts)
    return fallback


def patient_initials(resource):
    """One or two initials for the avatar, or '?' when there is no name."""
    name = patient_display_name(resource, fallback='')
    parts = [part for part in name.split() if part]
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def calculate_age(birth_date):
    """Age in whole years from a FHIR birthDate, or None."""
    parsed = parse_fhir_datetime(birth_date)
    if parsed is None:
        return None
    today = datetime.now()
    return today.year - parsed.year - (
        (today.month, today.day) < (parsed.month, parsed.day)
    )


def format_address(address):
    """Flatten a FHIR Address into a single line."""
    if not isinstance(address, dict):
        return ''
    # Copy the list -- extending the value returned by .get() would mutate the
    # caller's resource in place.
    parts = list(address.get('line') or [])
    parts.extend([
        address.get('city', ''),
        address.get('state', ''),
        address.get('postalCode', ''),
    ])
    return ', '.join(part for part in parts if part)


def first_coding_display(concept, fallback=''):
    """Pull a human label out of a CodeableConcept.

    Prefers `text`, then the first coding's `display`, then its raw `code`.
    """
    if not isinstance(concept, dict):
        return fallback
    if concept.get('text'):
        return concept['text']
    for coding in concept.get('coding') or []:
        if not isinstance(coding, dict):
            continue
        if coding.get('display'):
            return coding['display']
        if coding.get('code'):
            return coding['code']
    return fallback


def observation_value(resource):
    """Render an Observation's value across the value[x] choice types.

    Observations use a choice element, so `valueQuantity` is only one of several
    possibilities. Panels and blood pressure carry no top-level value at all --
    their readings live in `component`.
    """
    if not isinstance(resource, dict):
        return '', ''

    quantity = resource.get('valueQuantity')
    if isinstance(quantity, dict) and quantity.get('value') is not None:
        return quantity['value'], quantity.get('unit', '')

    if 'valueCodeableConcept' in resource:
        return first_coding_display(resource['valueCodeableConcept'], fallback='-'), ''

    if resource.get('valueString'):
        return resource['valueString'], ''

    if isinstance(resource.get('valueBoolean'), bool):
        return 'Yes' if resource['valueBoolean'] else 'No', ''

    if resource.get('valueInteger') is not None:
        return resource['valueInteger'], ''

    ratio = resource.get('valueRatio')
    if isinstance(ratio, dict):
        numerator = (ratio.get('numerator') or {}).get('value')
        denominator = (ratio.get('denominator') or {}).get('value')
        if numerator is not None and denominator is not None:
            return f'{numerator}/{denominator}', (ratio.get('numerator') or {}).get('unit', '')

    value_range = resource.get('valueRange')
    if isinstance(value_range, dict):
        low = (value_range.get('low') or {}).get('value')
        high = (value_range.get('high') or {}).get('value')
        if low is not None or high is not None:
            return f'{low if low is not None else ""}-{high if high is not None else ""}', \
                   (value_range.get('low') or value_range.get('high') or {}).get('unit', '')

    # Component-bearing observations (blood pressure, panels).
    components = resource.get('component') or []
    rendered = []
    unit = ''
    for component in components:
        if not isinstance(component, dict):
            continue
        label = first_coding_display(component.get('code', {}), fallback='')
        value, component_unit = observation_value(component)
        if value == '':
            continue
        unit = unit or component_unit
        rendered.append(f'{label}: {value}' if label else str(value))
    if rendered:
        return ' / '.join(rendered), unit

    if resource.get('dataAbsentReason'):
        return first_coding_display(resource['dataAbsentReason'], fallback='Not recorded'), ''

    return '', ''


def observation_code(resource):
    """Human-readable label for what an Observation measured."""
    return first_coding_display(resource.get('code', {}), fallback='Unknown')


# LOINC codes for systolic/diastolic blood pressure. Some servers record these
# as components of a panel Observation (code 85354-9); others record them as
# two independent Observations under these codes directly, sharing an
# encounter but no panel wrapper. Public so app.py can query for the second
# shape directly, not just extract it from a panel's components.
LOINC_SYSTOLIC = '8480-6'
LOINC_DIASTOLIC = '8462-4'


def blood_pressure_components(resource):
    """Pull numeric systolic/diastolic values out of a blood-pressure Observation.

    Returns (systolic, diastolic), either or both None if not present or not
    numeric. Used for charting, where a combined display string like the one
    `observation_value` produces for the table isn't usable.
    """
    systolic = diastolic = None
    for component in resource.get('component') or []:
        if not isinstance(component, dict):
            continue
        coding = (component.get('code') or {}).get('coding') or []
        codes = {c.get('code') for c in coding if isinstance(c, dict)}
        display = first_coding_display(component.get('code', {})).lower()
        value = (component.get('valueQuantity') or {}).get('value')
        if value is None:
            continue
        if LOINC_SYSTOLIC in codes or 'systolic' in display:
            systolic = value
        elif LOINC_DIASTOLIC in codes or 'diastolic' in display:
            diastolic = value
    return systolic, diastolic


def condition_onset(resource):
    """Render a Condition's onset (a FHIR choice type) for display and sorting.

    Falls back to `recordedDate` at the call site when onset is absent
    entirely -- onset is frequently missing on real data (roughly 1 in 4 on the
    public HAPI server), while recordedDate is a reasonable proxy for "when did
    this appear on the record."
    """
    # Sort values go through sort_key(), never parse_fhir_datetime() raw: the
    # raw parse keeps the timezone (HAPI onsets routinely carry one), while the
    # recordedDate fallback the caller mixes in is naive -- and Python refuses
    # to compare aware with naive, crashing the whole panel sort.
    if 'onsetDateTime' in resource:
        value = resource['onsetDateTime']
        return format_fhir_date(value), sort_key(value)

    if isinstance(resource.get('onsetPeriod'), dict):
        start = resource['onsetPeriod'].get('start')
        if start:
            return format_fhir_date(start), sort_key(start)

    if resource.get('onsetString'):
        return resource['onsetString'], None

    if isinstance(resource.get('onsetAge'), dict):
        age_value = resource['onsetAge'].get('value')
        if age_value is not None:
            return f'Age {age_value}', None

    return None, None
