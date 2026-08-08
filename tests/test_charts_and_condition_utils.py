from charts import bar_chart, demographics_charts, line_chart, vitals_charts
from fhir_utils import blood_pressure_components, condition_onset


class TestBloodPressureComponents:
    def test_matches_by_loinc_code(self):
        resource = {'component': [
            {'code': {'coding': [{'system': 'http://loinc.org', 'code': '8480-6'}]},
             'valueQuantity': {'value': 130}},
            {'code': {'coding': [{'system': 'http://loinc.org', 'code': '8462-4'}]},
             'valueQuantity': {'value': 85}},
        ]}
        assert blood_pressure_components(resource) == (130, 85)

    def test_falls_back_to_display_text(self):
        resource = {'component': [
            {'code': {'text': 'Systolic reading'}, 'valueQuantity': {'value': 118}},
            {'code': {'text': 'Diastolic reading'}, 'valueQuantity': {'value': 76}},
        ]}
        assert blood_pressure_components(resource) == (118, 76)

    def test_missing_components_return_none(self):
        assert blood_pressure_components({}) == (None, None)
        assert blood_pressure_components({'component': []}) == (None, None)

    def test_partial_data_returns_one_none(self):
        resource = {'component': [
            {'code': {'coding': [{'code': '8480-6'}]}, 'valueQuantity': {'value': 140}},
        ]}
        systolic, diastolic = blood_pressure_components(resource)
        assert systolic == 140
        assert diastolic is None


class TestConditionOnset:
    def test_onset_datetime(self):
        display, sort_value = condition_onset({'onsetDateTime': '2020-03-15'})
        assert display == '2020-03-15'
        assert sort_value is not None

    def test_onset_period_uses_start(self):
        display, _sort = condition_onset(
            {'onsetPeriod': {'start': '2019-01-01', 'end': '2019-06-01'}}
        )
        assert display == '2019-01-01'

    def test_onset_string(self):
        display, sort_value = condition_onset({'onsetString': 'childhood'})
        assert display == 'childhood'
        assert sort_value is None

    def test_onset_age(self):
        display, _sort = condition_onset({'onsetAge': {'value': 12}})
        assert display == 'Age 12'

    def test_no_onset_at_all_returns_none(self):
        assert condition_onset({}) == (None, None)
        assert condition_onset({'recordedDate': '2020-01-01'}) == (None, None)


class TestChartBuilders:
    def test_single_dataset_line_chart_has_no_legend(self):
        config = line_chart(['d1'], [{'label': 'HR', 'values': [70], 'color': '#2a78d6'}])
        assert config['options']['plugins']['legend']['display'] is False

    def test_two_dataset_line_chart_has_legend(self):
        config = line_chart(['d1'], [
            {'label': 'Systolic', 'values': [120], 'color': '#2a78d6'},
            {'label': 'Diastolic', 'values': [80], 'color': '#eb6834'},
        ])
        assert config['options']['plugins']['legend']['display'] is True

    def test_bar_chart_is_single_hue_not_categorical(self):
        """The job is comparing magnitude across categories, not telling
        multiple series apart, so this must stay one color -- not a
        categorical palette per bar."""
        config = bar_chart(['Male', 'Female'], [10, 12])
        assert config['data']['datasets'][0]['backgroundColor'] == '#2a78d6'
        assert len(config['data']['datasets']) == 1

    def test_bar_chart_has_integer_ticks(self):
        config = bar_chart(['a', 'b'], [1, 2])
        assert config['options']['scales']['y']['ticks']['precision'] == 0

    def test_vitals_charts_skips_empty_series(self):
        series = {
            'heart_rate': {'label': 'Heart Rate', 'unit': 'bpm', 'rows': []},
            'temperature': {'label': 'Temperature', 'unit': '°C',
                            'rows': [(1, '2026-01-01', 37.0)]},
        }
        charts = vitals_charts(series)
        assert 'heart_rate' not in charts
        assert 'temperature' in charts

    def test_vitals_charts_sorts_ascending_by_first_element(self):
        series = {
            'heart_rate': {'label': 'HR', 'unit': 'bpm', 'rows': [
                (2, '2026-01-02', 80),
                (1, '2026-01-01', 60),
            ]},
        }
        charts = vitals_charts(series)
        assert charts['heart_rate']['data']['datasets'][0]['data'] == [60, 80]

    def test_demographics_charts_shape(self):
        demographics = {
            'gender_counts': {'male': 1, 'female': 2, 'other': 0, 'unknown': 0},
            'age_groups': {'0-18': 0, '19-35': 1, '36-55': 1, '56-75': 0, '76+': 0, 'unknown': 1},
        }
        charts = demographics_charts(demographics)
        assert charts['gender']['data']['datasets'][0]['data'] == [1, 2, 0, 0]
        assert charts['age']['data']['datasets'][0]['data'] == [0, 1, 1, 0, 0, 1]
