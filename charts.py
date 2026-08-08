"""Chart.js configuration builders.

Keeps color and mark choices in one place, separate from FHIR parsing
(fhir_utils.py) and route orchestration (app.py). Colors are the validated
default palette's light-mode steps -- this app has no dark theme, so only
light-mode values are needed (see dataviz skill, references/palette.md).
"""

BLUE = '#2a78d6'    # categorical slot 1 -- primary series, single-series default
ORANGE = '#eb6834'  # categorical slot 2 -- second series (diastolic vs systolic)
GRID = '#e1e0d9'    # hairline gridline
MUTED = '#898781'   # axis / tick / legend text
SURFACE = '#fcfcfb'  # chart surface -- used for the marker ring


def _base_options(unit_label=None, show_legend=True, integer_ticks=False):
    y_ticks = {'color': MUTED}
    if integer_ticks:
        y_ticks['precision'] = 0
    return {
        'responsive': True,
        'maintainAspectRatio': False,
        # 'index' mode + intersect:false gives a crosshair-style tooltip that
        # lists every series at the hovered x position, not just the point
        # directly under the pointer.
        'interaction': {'mode': 'index', 'intersect': False},
        'plugins': {
            'legend': {
                'display': show_legend,
                'labels': {'usePointStyle': True, 'boxWidth': 8, 'color': MUTED},
            },
            'tooltip': {'mode': 'index', 'intersect': False},
        },
        'scales': {
            'x': {'grid': {'display': False}, 'ticks': {'color': MUTED}},
            'y': {
                'grid': {'color': GRID},
                'ticks': y_ticks,
                'title': {'display': bool(unit_label), 'text': unit_label or '', 'color': MUTED},
            },
        },
    }


def _line_dataset(label, values, color):
    return {
        'label': label,
        'data': values,
        'borderColor': color,
        'backgroundColor': color,
        'borderWidth': 2,
        'pointRadius': 4,
        'pointBackgroundColor': color,
        'pointBorderColor': SURFACE,
        'pointBorderWidth': 2,
        'tension': 0.15,
        'fill': False,
    }


def line_chart(labels, datasets, unit=None):
    """A trend-over-time chart. `datasets` is [{'label', 'values', 'color'}, ...].

    A single dataset gets no legend box -- the card's own title already says
    what is plotted, per the "single series needs no legend" rule.
    """
    show_legend = len(datasets) > 1
    return {
        'type': 'line',
        'data': {
            'labels': labels,
            'datasets': [_line_dataset(d['label'], d['values'], d['color']) for d in datasets],
        },
        'options': _base_options(unit_label=unit, show_legend=show_legend),
    }


def bar_chart(labels, values, unit=None):
    """A magnitude comparison across categories -- one hue, not one per bar.

    The job is "compare counts across categories," not "tell series apart," so
    this stays a single sequential hue rather than a multi-color categorical
    palette (see dataviz skill, choosing-a-form.md).
    """
    return {
        'type': 'bar',
        'data': {
            'labels': labels,
            'datasets': [{
                'label': unit or 'Count',
                'data': values,
                'backgroundColor': BLUE,
                'borderRadius': 4,
                'borderSkipped': 'bottom',
                'maxBarThickness': 24,
            }],
        },
        'options': _base_options(unit_label=unit, show_legend=False, integer_ticks=True),
    }


def vitals_charts(series):
    """Build one chart per vital-sign type that actually has data.

    `series` maps an internal key to {'label', 'unit', 'rows'}, where each row
    is (sort_datetime, date_label, value) -- or, for blood pressure,
    (sort_datetime, date_label, systolic, diastolic). Charts read left-to-right
    oldest-to-newest, so rows are sorted ascending here regardless of the
    order the caller collected them in.
    """
    charts = {}
    for key, info in series.items():
        rows = sorted(info['rows'], key=lambda row: row[0])
        if not rows:
            continue
        labels = [row[1] for row in rows]
        if key == 'blood_pressure':
            charts[key] = line_chart(labels, [
                {'label': 'Systolic', 'values': [row[2] for row in rows], 'color': BLUE},
                {'label': 'Diastolic', 'values': [row[3] for row in rows], 'color': ORANGE},
            ], unit=info['unit'])
        else:
            charts[key] = line_chart(labels, [
                {'label': info['label'], 'values': [row[2] for row in rows], 'color': BLUE},
            ], unit=info['unit'])
    return charts


def demographics_charts(demographics):
    """Gender and age-group bar charts from the same dict the tables render."""
    gender_counts = demographics['gender_counts']
    age_groups = demographics['age_groups']
    return {
        'gender': bar_chart(
            ['Male', 'Female', 'Other', 'Unknown'],
            [gender_counts['male'], gender_counts['female'],
             gender_counts['other'], gender_counts['unknown']],
            unit='Patients',
        ),
        'age': bar_chart(
            ['0-18', '19-35', '36-55', '56-75', '76+', 'Unknown'],
            [age_groups['0-18'], age_groups['19-35'], age_groups['36-55'],
             age_groups['56-75'], age_groups['76+'], age_groups['unknown']],
            unit='Patients',
        ),
    }
