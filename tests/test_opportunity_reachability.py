import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from opportunity_planner.opportunities import (
    ReachabilityAnalyzer,
    ReachabilityError,
    ReachStatus,
)

UTC = dt.timezone.utc


def scenario(**overrides):
    data = {
        'scenario': 'baseline',
        'as_of': '2026-11-16T00:00:00+00:00',
        'minimum_rest_minutes': 30,
        'travel_matrix': {'school': {'job': 20}, 'job': {'school': 20}},
        'courses': [
            {
                'course_id': 'basic',
                'start': '2026-10-01T09:00:00+00:00',
                'end': '2026-11-15T17:00:00+00:00',
                'location': 'school',
                'capacity': 20,
                'enrolled': 5,
            }
        ],
        'interviews': [
            {
                'interview_id': 'int-1',
                'start': '2026-11-17T09:00:00+00:00',
                'end': '2026-11-17T10:00:00+00:00',
                'location': 'job',
                'capacity': 5,
                'booked': 1,
            }
        ],
        'shifts': [
            {
                'shift_id': 'shift-1',
                'start': '2026-11-17T11:00:00+00:00',
                'end': '2026-11-17T14:00:00+00:00',
                'location': 'job',
                'capacity': 2,
                'filled': 0,
            }
        ],
        'people': [
            {
                'person_id': 'person-1',
                'cohort_id': 'cohort-a',
                'course_id': 'basic',
                'completed_at': '2026-11-15T17:00:00+00:00',
                'completed_courses': ['basic'],
                'segment': 'youth',
            },
            {
                'person_id': 'person-2',
                'cohort_id': 'cohort-a',
                'course_id': 'basic',
                'segment': 'youth',
            },
        ],
        'cohorts': [
            {'cohort_id': 'cohort-a', 'course_id': 'basic', 'segment': 'youth'},
            {'cohort_id': 'cohort-b', 'course_id': 'basic', 'segment': 'women'},
        ],
    }
    data.update(overrides)
    return data


def person(**overrides):
    value = {
        'person_id': 'person-1',
        'cohort_id': 'cohort-a',
        'course_id': 'basic',
        'completed_at': '2026-11-15T17:00:00+00:00',
        'completed_courses': ['basic'],
        'segment': 'youth',
    }
    value.update(overrides)
    return value


def by_person(report):
    return {row['person_id']: row for row in report['persons']}


def links_for(report, person_id):
    return [link for link in report['links'] if link['person_id'] == person_id]


class ReachabilityBaselineTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = ReachabilityAnalyzer()

    def test_completed_person_with_room_is_fulfilled(self):
        report = self.analyzer.analyze(scenario()).as_dict()
        person_one = by_person(report)['person-1']
        self.assertEqual(person_one['status'], ReachStatus.FULFILLED.value)
        self.assertEqual(person_one['realized_count'], 1)
        self.assertEqual(person_one['realized_opportunity_id'], 'opp-int-1-shift-1')
        self.assertEqual(report['totals']['realized_arrangements'], 1)

    def test_no_completion_evidence_is_theoretical_and_never_counted(self):
        report = self.analyzer.analyze(scenario()).as_dict()
        person_two = by_person(report)['person-2']
        self.assertEqual(person_two['status'], ReachStatus.THEORETICAL.value)
        self.assertEqual(person_two['realized_count'], 0)
        self.assertIn('course_not_completed', person_two['blockers'])
        self.assertEqual(report['totals']['theoretical_arrangements'], 1)
        self.assertEqual(report['totals']['realized_arrangements'], 1)
        for link in links_for(report, 'person-2'):
            self.assertFalse(link['counted'])

    def test_person_satisfying_many_opportunities_counts_once(self):
        data = scenario()
        data['shifts'].append(
            {
                'shift_id': 'shift-2',
                'start': '2026-11-18T11:00:00+00:00',
                'end': '2026-11-18T14:00:00+00:00',
                'location': 'job',
                'capacity': 3,
                'filled': 0,
            }
        )
        report = self.analyzer.analyze(data).as_dict()
        person_one = by_person(report)['person-1']
        self.assertEqual(person_one['realized_count'], 1)
        self.assertEqual(len(person_one['suppressed_opportunities']), 1)
        self.assertEqual(report['totals']['realized_arrangements'], 1)
        self.assertEqual(report['totals']['suppressed_links'], 1)
        self.assertEqual(len(report['suppressed_links']), 1)

    def test_analysis_does_not_mutate_input(self):
        data = scenario()
        snapshot = copy.deepcopy(data)
        report = self.analyzer.analyze(data).as_dict()
        self.assertEqual(data, snapshot)
        self.assertTrue(report['read_only']['inputs_unchanged'])
        self.assertEqual(report['read_only']['input_fingerprint'], report['read_only']['fingerprint_after'])
        self.assertEqual(report['read_only']['storage_writes'], 0)

    def test_totals_rates_are_strings(self):
        report = self.analyzer.analyze(scenario()).as_dict()
        self.assertEqual(report['totals']['rates']['completion_rate'], '0.5000')
        self.assertEqual(report['totals']['rates']['reach_rate'], '0.5000')
        self.assertEqual(report['totals']['rates']['fulfilled_after_completion_rate'], '1.0000')


class ReachabilityTimeTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = ReachabilityAnalyzer()

    def test_missed_interview_window_is_invalid_with_path(self):
        data = scenario(
            interviews=[
                {
                    'interview_id': 'int-early',
                    'start': '2026-11-15T17:10:00+00:00',
                    'end': '2026-11-15T17:40:00+00:00',
                    'location': 'job',
                    'capacity': 5,
                    'booked': 0,
                }
            ],
            shifts=[],
            people=[person()],
        )
        report = self.analyzer.analyze(data).as_dict()
        link = links_for(report, 'person-1')[0]
        self.assertEqual(link['status'], ReachStatus.INVALID.value)
        self.assertIn('interview_window_missed', link['reasons'])
        self.assertEqual(link['failed_step'], 'interview_window')
        self.assertIn('travel_course_interview', [step.get('leg') for step in link['path'] if step['step'] == 'travel'])

    def test_shift_window_missed_after_interview(self):
        data = scenario(
            shifts=[
                {
                    'shift_id': 'shift-tight',
                    'start': '2026-11-17T10:00:00+00:00',
                    'end': '2026-11-17T12:00:00+00:00',
                    'location': 'job',
                    'capacity': 2,
                    'filled': 0,
                }
            ],
            people=[person()],
        )
        report = self.analyzer.analyze(data).as_dict()
        link = links_for(report, 'person-1')[0]
        self.assertIn('shift_window_missed', link['reasons'])
        self.assertEqual(link['failed_step'], 'shift_window')

    def test_missing_travel_direction_is_blocked_not_success(self):
        data = scenario(travel_matrix={}, people=[person()])
        report = self.analyzer.analyze(data).as_dict()
        link = links_for(report, 'person-1')[0]
        self.assertEqual(link['status'], ReachStatus.BLOCKED.value)
        self.assertFalse(link['counted'])
        self.assertTrue(any(reason.startswith('travel_unknown') for reason in link['reasons']))
        self.assertEqual(report['totals']['realized_arrangements'], 0)

    def test_reverse_only_travel_is_insufficient(self):
        data = scenario(travel_matrix={'job': {'school': 20}}, people=[person()])
        report = self.analyzer.analyze(data).as_dict()
        link = links_for(report, 'person-1')[0]
        self.assertIn('travel_unknown:school->job', link['reasons'])
        self.assertFalse(link['counted'])

    def test_booking_window_closed_invalidates_shift(self):
        data = scenario(
            shifts=[
                {
                    'shift_id': 'shift-old',
                    'start': '2026-11-17T11:00:00+00:00',
                    'end': '2026-11-17T14:00:00+00:00',
                    'location': 'job',
                    'capacity': 2,
                    'filled': 0,
                    'open_until': '2026-11-16T18:00:00+00:00',
                }
            ],
            people=[person()],
        )
        report = self.analyzer.analyze(data).as_dict()
        link = links_for(report, 'person-1')[0]
        self.assertIn('shift_booking_window_closed', link['reasons'])
        self.assertFalse(link['counted'])

    def test_commitment_conflict_blocks_arrangement(self):
        data = scenario(
            people=[person()],
            commitments=[
                {
                    'commitment_id': 'c-1',
                    'person_id': 'person-1',
                    'start': '2026-11-17T09:30:00+00:00',
                    'end': '2026-11-17T10:30:00+00:00',
                }
            ],
        )
        report = self.analyzer.analyze(data).as_dict()
        link = links_for(report, 'person-1')[0]
        self.assertEqual(link['status'], ReachStatus.INVALID.value)
        self.assertTrue(any(reason.startswith('commitment_conflict') for reason in link['reasons']))
        self.assertFalse(link['counted'])


class ReachabilityCapacityTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = ReachabilityAnalyzer()

    def completed(self, identifier):
        return person(person_id=identifier, completed_at='2026-11-15T17:00:00+00:00')

    def test_exhausted_shift_capacity_is_invalid(self):
        data = scenario(
            shifts=[
                {
                    'shift_id': 'shift-full',
                    'start': '2026-11-17T11:00:00+00:00',
                    'end': '2026-11-17T14:00:00+00:00',
                    'location': 'job',
                    'capacity': 1,
                    'filled': 1,
                }
            ],
            people=[person()],
        )
        report = self.analyzer.analyze(data).as_dict()
        link = links_for(report, 'person-1')[0]
        self.assertIn('shift_capacity_exhausted', link['reasons'])
        self.assertFalse(link['counted'])

    def test_scarce_capacity_marks_later_person_capacity_lost(self):
        data = scenario(
            people=[self.completed('person-1'), self.completed('person-2')],
            shifts=[
                {
                    'shift_id': 'shift-1',
                    'start': '2026-11-17T11:00:00+00:00',
                    'end': '2026-11-17T14:00:00+00:00',
                    'location': 'job',
                    'capacity': 1,
                    'filled': 0,
                }
            ],
            interviews=[
                {
                    'interview_id': 'int-1',
                    'start': '2026-11-17T09:00:00+00:00',
                    'end': '2026-11-17T10:00:00+00:00',
                    'location': 'job',
                    'capacity': 5,
                    'booked': 0,
                }
            ],
        )
        report = self.analyzer.analyze(data).as_dict()
        rows = by_person(report)
        self.assertEqual(rows['person-1']['status'], ReachStatus.FULFILLED.value)
        self.assertEqual(rows['person-2']['status'], ReachStatus.INVALID.value)
        self.assertEqual(rows['person-2']['blockers'].get('capacity_allocated_elsewhere'), 1)
        self.assertEqual(report['totals']['realized_arrangements'], 1)
        self.assertEqual(report['totals']['capacity_lost_links'], 1)
        self.assertTrue(report['capacity_pressure'])

    def test_unknown_capacity_is_blocked(self):
        data = scenario(
            people=[person()],
            shifts=[
                {
                    'shift_id': 'shift-unknown',
                    'start': '2026-11-17T11:00:00+00:00',
                    'end': '2026-11-17T14:00:00+00:00',
                    'location': 'job',
                }
            ],
        )
        report = self.analyzer.analyze(data).as_dict()
        link = links_for(report, 'person-1')[0]
        self.assertEqual(link['status'], ReachStatus.BLOCKED.value)
        self.assertIn('shift_capacity_unknown', link['reasons'])
        self.assertFalse(link['counted'])

    def test_allocation_is_analysis_only_and_keeps_snapshot(self):
        report = self.analyzer.analyze(scenario()).as_dict()
        allocation = {row['resource_id']: row for row in report['capacity_allocation']}
        self.assertEqual(allocation['shift-1']['remaining_at_start'], 2)
        self.assertEqual(allocation['shift-1']['remaining_after_allocation'], 1)
        self.assertEqual(report['read_only']['storage_writes'], 0)

    def test_totals_are_consistent_with_links(self):
        data = scenario(
            people=[
                self.completed('person-1'),
                self.completed('person-2'),
                self.completed('person-3'),
            ]
        )
        report = self.analyzer.analyze(data).as_dict()
        counted = [link for link in report['links'] if link['counted']]
        self.assertEqual(len(counted), report['totals']['realized_arrangements'])
        feasible = [link for link in report['links'] if link['individual_status'] == 'fulfilled']
        self.assertEqual(
            len(feasible),
            report['totals']['realized_arrangements']
            + report['totals']['suppressed_links']
            + report['totals']['capacity_lost_links'],
        )
        self.assertEqual(
            len([link for link in report['links'] if 'capacity_allocated_elsewhere' in link['reasons']]),
            report['totals']['capacity_lost_links'],
        )


class ReachabilityGroupTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = ReachabilityAnalyzer()

    def data(self):
        data = scenario(
            people=[
                person(),
                person(person_id='person-2', completed_courses=[], segment='women', cohort_id='cohort-b'),
                person(person_id='person-3', completed_at='2026-11-15T17:00:00+00:00', segment='women', cohort_id='cohort-b'),
            ]
        )
        return data

    def test_by_course_and_cohort(self):
        report = self.analyzer.analyze(self.data()).as_dict()
        self.assertIn('basic', report['by_course'])
        self.assertEqual(report['by_course']['basic']['people'], 3)
        self.assertIn('cohort-a', report['by_cohort'])
        self.assertIn('cohort-b', report['by_cohort'])
        self.assertEqual(report['by_cohort']['cohort-b']['realized'], 1)

    def test_by_segment_rates(self):
        report = self.analyzer.analyze(self.data()).as_dict()
        self.assertIn('women', report['by_segment'])
        self.assertIn('youth', report['by_segment'])
        self.assertEqual(report['by_segment']['youth']['realized'], 1)

    def test_blocker_categories_split_time_and_capacity(self):
        data = scenario(
            people=[person(), person(person_id='person-2', completed_at='2026-11-15T17:00:00+00:00')],
            shifts=[
                {
                    'shift_id': 'shift-1',
                    'start': '2026-11-17T11:00:00+00:00',
                    'end': '2026-11-17T14:00:00+00:00',
                    'location': 'job',
                    'capacity': 1,
                    'filled': 0,
                }
            ],
        )
        report = self.analyzer.analyze(data).as_dict()
        categories = report['by_course']['basic']['blocker_categories']
        self.assertIn('capacity', categories)

    def test_cohort_detail_traces_paths(self):
        report = self.analyzer.analyze(self.data())
        detail = self.analyzer.cohort_detail(report, 'cohort-a')
        self.assertEqual(detail['cohort_id'], 'cohort-a')
        self.assertTrue(detail['links'])
        self.assertTrue(all(link['cohort_id'] == 'cohort-a' for link in detail['links']))
        with self.assertRaises(ReachabilityError):
            self.analyzer.cohort_detail(report, 'missing')

    def test_person_detail_traces_blocked_reason(self):
        data = scenario(travel_matrix={}, people=[person()])
        report = self.analyzer.analyze(data)
        detail = self.analyzer.person_detail(report, 'person-1')
        self.assertEqual(detail['person']['status'], ReachStatus.BLOCKED.value)
        self.assertTrue(detail['blocked_paths'])
        self.assertTrue(detail['blocked_paths'][0]['reasons'])

    def test_unknown_person_raises(self):
        report = self.analyzer.analyze(scenario())
        with self.assertRaises(ReachabilityError):
            self.analyzer.person_detail(report, 'missing')


class ScenarioComparisonTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = ReachabilityAnalyzer()

    def test_earlier_course_creates_more_realized_arrangements(self):
        early = scenario(
            courses=[
                {
                    'course_id': 'basic',
                    'start': '2026-10-01T09:00:00+00:00',
                    'end': '2026-11-10T17:00:00+00:00',
                    'location': 'school',
                    'capacity': 20,
                    'enrolled': 5,
                }
            ],
            people=[person(completed_at='2026-11-10T17:00:00+00:00')],
        )
        late = scenario(
            courses=[
                {
                    'course_id': 'basic',
                    'start': '2026-10-20T09:00:00+00:00',
                    'end': '2026-11-20T17:00:00+00:00',
                    'location': 'school',
                    'capacity': 20,
                    'enrolled': 5,
                }
            ],
            people=[person(completed_at='2026-11-20T17:00:00+00:00')],
        )
        result = self.analyzer.compare(early, late)
        self.assertEqual(result['deltas']['realized_arrangements'], -1)
        self.assertEqual(result['persons']['lost'][0]['person_id'], 'person-1')
        self.assertEqual(result['left']['scenario'], 'baseline')
        self.assertIn('basic', result['by_course'])

    def test_comparison_of_identical_scenarios_has_no_gain_or_loss(self):
        result = self.analyzer.compare(scenario(), scenario())
        self.assertEqual(result['persons']['gained'], [])
        self.assertEqual(result['persons']['lost'], [])
        self.assertEqual(result['deltas']['realized_arrangements'], 0)

    def test_comparison_reports_missing_person(self):
        right = scenario(people=[person(person_id='person-9')])
        result = self.analyzer.compare(scenario(), right)
        self.assertTrue(result['persons']['lost'] or result['persons']['gained'])

    def test_comparison_breaks_down_course_and_segment(self):
        right = scenario(
            travel_matrix={'job': {'school': 20}},
            people=[person()],
        )
        result = self.analyzer.compare(scenario(people=[person()]), right)
        self.assertIn('basic', result['by_course_segment'])
        self.assertIn('youth', result['by_course_segment']['basic'])
        entry = result['by_course_segment']['basic']['youth']
        self.assertEqual(entry['left_realized'], 1)
        self.assertEqual(entry['right_realized'], 0)
        self.assertEqual(entry['delta_realized'], -1)

    def test_comparison_exposes_schedule_shift_effect(self):
        early_shift = scenario(
            people=[person()],
            shifts=[
                {
                    'shift_id': 'shift-1',
                    'start': '2026-11-17T11:00:00+00:00',
                    'end': '2026-11-17T14:00:00+00:00',
                    'location': 'job',
                    'capacity': 2,
                    'filled': 0,
                }
            ],
        )
        late_shift = scenario(
            people=[person()],
            shifts=[
                {
                    'shift_id': 'shift-1',
                    'start': '2026-11-17T10:05:00+00:00',
                    'end': '2026-11-17T13:00:00+00:00',
                    'location': 'job',
                    'capacity': 2,
                    'filled': 0,
                }
            ],
        )
        result = self.analyzer.compare(early_shift, late_shift)
        self.assertEqual(result['deltas']['realized_arrangements'], -1)
        self.assertEqual(result['opportunities']['opp-int-1-shift-1']['realized_right'], 0)
        self.assertEqual(result['opportunities']['opp-int-1-shift-1']['realized_left'], 1)


class ReachabilityValidationTests(unittest.TestCase):
    def setUp(self):
        self.analyzer = ReachabilityAnalyzer()

    def test_empty_people_rejected(self):
        with self.assertRaises(ReachabilityError):
            self.analyzer.analyze(scenario(people=[]))

    def test_reversed_course_interval_rejected(self):
        data = scenario(
            courses=[
                {
                    'course_id': 'basic',
                    'start': '2026-11-20T09:00:00+00:00',
                    'end': '2026-11-01T17:00:00+00:00',
                }
            ]
        )
        with self.assertRaises(ReachabilityError):
            self.analyzer.analyze(data)

    def test_non_mapping_input_rejected(self):
        with self.assertRaises(ReachabilityError):
            self.analyzer.analyze([])

    def test_explicit_opportunity_is_respected(self):
        data = scenario(
            people=[person()],
            opportunities=[
                {
                    'opportunity_id': 'opp-manual',
                    'cohort_id': 'cohort-a',
                    'interview': {
                        'interview_id': 'int-manual',
                        'start': '2026-11-17T09:00:00+00:00',
                        'end': '2026-11-17T10:00:00+00:00',
                        'location': 'job',
                        'capacity': 3,
                        'booked': 0,
                    },
                    'shift': {
                        'shift_id': 'shift-manual',
                        'start': '2026-11-17T11:00:00+00:00',
                        'end': '2026-11-17T14:00:00+00:00',
                        'location': 'job',
                        'capacity': 3,
                        'filled': 0,
                    },
                }
            ],
        )
        report = self.analyzer.analyze(data).as_dict()
        self.assertEqual(
            [link['opportunity_id'] for link in report['links']],
            ['opp-manual'],
        )

    def test_person_without_course_is_excluded_with_reason(self):
        data = scenario(
            people=[
                person(),
                {'person_id': 'person-x', 'segment': 'youth', 'completed_at': '2026-11-15T17:00:00+00:00'},
            ]
        )
        report = self.analyzer.analyze(data).as_dict()
        self.assertEqual([item['person_id'] for item in report['excluded_people']], ['person-x'])
        self.assertEqual(report['excluded_people'][0]['reason'], 'course_not_assigned')
        self.assertEqual(report['totals']['persons'], 1)

    def test_independent_mode_keeps_reachable_gig_after_missed_interview(self):
        data = scenario(
            people=[person()],
            opportunity_mode='independent',
            interviews=[
                {
                    'interview_id': 'int-early',
                    'start': '2026-11-15T17:10:00+00:00',
                    'end': '2026-11-15T17:40:00+00:00',
                    'location': 'job',
                    'capacity': 5,
                    'booked': 0,
                }
            ],
        )
        report = self.analyzer.analyze(data).as_dict()
        statuses = by_person(report)['person-1']['opportunity_statuses']
        self.assertEqual(statuses['opp-int-early'], ReachStatus.INVALID.value)
        self.assertEqual(statuses['opp-shift-1'], ReachStatus.FULFILLED.value)
        self.assertEqual(report['totals']['realized_arrangements'], 1)

    def test_chain_mode_requires_both_stages(self):
        data = scenario(
            people=[person()],
            interviews=[
                {
                    'interview_id': 'int-early',
                    'start': '2026-11-15T17:10:00+00:00',
                    'end': '2026-11-15T17:40:00+00:00',
                    'location': 'job',
                    'capacity': 5,
                    'booked': 0,
                }
            ],
        )
        report = self.analyzer.analyze(data).as_dict()
        self.assertEqual(report['totals']['realized_arrangements'], 0)
        self.assertEqual(len(report['links']), 1)
        self.assertEqual(report['links'][0]['kind'], 'bundle')

    def test_unknown_opportunity_mode_rejected(self):
        with self.assertRaises(ReachabilityError):
            self.analyzer.analyze(scenario(opportunity_mode='unsorted'))

    def test_prerequisite_blocks_chain(self):
        data = scenario(
            courses=[
                {
                    'course_id': 'basic',
                    'start': '2026-10-01T09:00:00+00:00',
                    'end': '2026-11-15T17:00:00+00:00',
                    'location': 'school',
                    'prerequisites': ['intro'],
                    'capacity': 20,
                    'enrolled': 5,
                }
            ],
            people=[person(completed_courses=[])],
        )
        report = self.analyzer.analyze(data).as_dict()
        link = links_for(report, 'person-1')[0]
        self.assertIn('course_prerequisite_missing:intro', link['reasons'])
        self.assertFalse(link['counted'])


class AdapterTests(unittest.TestCase):
    def test_acceptance_schedule_becomes_reachability_input(self):
        from opportunity_planner.opportunities.adapters import from_schedule

        payload = json.loads(
            (Path(__file__).resolve().parent.parent / 'fixtures' / 'acceptance.json').read_text(encoding='utf-8')
        )
        adapted = from_schedule(payload)
        self.assertEqual(adapted['people'][0]['person_id'], 'person-1')
        self.assertEqual([course['course_id'] for course in adapted['courses']], ['course'])
        self.assertEqual([shift['shift_id'] for shift in adapted['shifts']], ['shift'])
        self.assertEqual(adapted['travel_matrix']['school']['job'], 20)

    def test_adapter_does_not_mark_activity_completion_without_timestamp(self):
        from opportunity_planner.opportunities.adapters import from_schedule

        payload = {
            'items': [
                {
                    'id': 'plan-1',
                    'owner_id': 'person-1',
                    'activities': [
                        {
                            'id': 'course',
                            'kind': 'training',
                            'start': '2026-11-16T09:00:00+00:00',
                            'end': '2026-11-16T10:00:00+00:00',
                            'location': 'school',
                            'completed': True,
                        }
                    ],
                }
            ]
        }
        adapted = from_schedule(payload)
        self.assertIsNone(adapted['people'][0]['completed_at'])
        report = ReachabilityAnalyzer().analyze(adapted).as_dict()
        self.assertEqual(report['totals']['realized_arrangements'], 0)
        self.assertEqual(report['totals']['completed_persons'], 0)

    def test_adapter_does_not_mutate_schedule(self):
        from opportunity_planner.opportunities.adapters import from_schedule

        payload = {
            'items': [
                {
                    'id': 'plan-1',
                    'owner_id': 'person-1',
                    'activities': [
                        {
                            'id': 'course',
                            'kind': 'training',
                            'start': '2026-11-16T09:00:00+00:00',
                            'end': '2026-11-16T10:00:00+00:00',
                            'location': 'school',
                        }
                    ],
                }
            ]
        }
        snapshot = copy.deepcopy(payload)
        from_schedule(payload)
        self.assertEqual(payload, snapshot)

    def test_from_records_reads_attributes_and_keeps_storage_untouched(self):
        from opportunity_planner.opportunities.adapters import from_records
        from opportunity_planner.storage import Database

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / 'db.sqlite3')

            record = {
                'person_id': 'person-1',
                'owner_id': 'owner-1',
                'category': 'scheduling',
                'status': 'active',
                'effective_from': '2026-11-16',
                'quantity': 0,
                'amount': '0',
                'attributes': {
                    'cohort_id': 'cohort-a',
                    'course_id': 'basic',
                    'completed_at': '2026-11-15T17:00:00+00:00',
                    'completed_courses': ['basic'],
                    'segment': 'youth',
                },
            }
            with database.transaction() as connection:
                database.put_record(connection, 'person', record)
            before = database.counts()
            data = from_records(
                courses=[
                    {
                        'course_id': 'basic',
                        'owner_id': 'owner-1',
                        'category': 'course',
                        'status': 'active',
                        'effective_from': '2026-10-01',
                        'effective_to': '2026-11-15',
                        'quantity': 10,
                        'amount': '0',
                        'attributes': {'start': '2026-10-01T09:00:00+00:00', 'end': '2026-11-15T17:00:00+00:00', 'location': 'school'},
                    }
                ],
                persons=database.list_records('person'),
                plans=[
                    {
                        'plan_id': 'plan-1',
                        'owner_id': 'owner-1',
                        'category': 'plan',
                        'status': 'active',
                        'effective_from': '2026-11-16',
                        'quantity': 0,
                        'amount': '0',
                        'attributes': {
                            'travel_matrix': {'school': {'job': 20}},
                            'minimum_rest_minutes': 30,
                        },
                    }
                ],
                interviews=[
                    {
                        'interview_id': 'int-1',
                        'owner_id': 'owner-1',
                        'category': 'interview',
                        'status': 'active',
                        'effective_from': '2026-11-17',
                        'quantity': 0,
                        'amount': '0',
                        'attributes': {
                            'start': '2026-11-17T09:00:00+00:00',
                            'end': '2026-11-17T10:00:00+00:00',
                            'location': 'job',
                            'capacity': 2,
                            'booked': 0,
                        },
                    }
                ],
                shifts=[
                    {
                        'shift_id': 'shift-1',
                        'owner_id': 'owner-1',
                        'category': 'shift',
                        'status': 'active',
                        'effective_from': '2026-11-17',
                        'quantity': 0,
                        'amount': '0',
                        'attributes': {
                            'start': '2026-11-17T11:00:00+00:00',
                            'end': '2026-11-17T14:00:00+00:00',
                            'location': 'job',
                            'capacity': 2,
                            'filled': 0,
                        },
                    }
                ],
            )
            report = ReachabilityAnalyzer().analyze(data).as_dict()
            self.assertEqual(report['totals']['realized_arrangements'], 1)
            self.assertEqual(database.counts(), before)
            self.assertEqual(database.pending_messages(), [])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding='utf-8')
        return str(path)

    def test_reachability_command_writes_output(self):
        from opportunity_planner.cli import main

        fixture = self.write('fixture.json', scenario())
        output = self.root / 'report.json'
        code = main(['reachability', '--fixture', fixture, '--output', str(output), '--cohort', 'cohort-a'])
        self.assertEqual(code, 0)
        payload = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(payload['scenario'], 'baseline')
        self.assertIn('cohort_detail', payload)
        self.assertEqual(payload['cohort_detail']['cohort_id'], 'cohort-a')
        self.assertTrue(payload['read_only']['inputs_unchanged'])

    def test_compare_command_reports_delta(self):
        from opportunity_planner.cli import main

        left = self.write('left.json', scenario())
        right = self.write('right.json', scenario(travel_matrix={}))
        output = self.root / 'compare.json'
        code = main(['compare-scenarios', '--left', left, '--right', right, '--output', str(output)])
        self.assertEqual(code, 0)
        payload = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(payload['deltas']['realized_arrangements'], -1)

    def test_reachability_does_not_touch_database(self):
        from opportunity_planner.cli import main

        fixture = self.write('fixture.json', scenario())
        output = self.root / 'report.json'
        database = self.root / 'unused.sqlite3'
        main(['--database', str(database), 'reachability', '--fixture', fixture, '--output', str(output)])
        self.assertFalse(database.exists())

    def test_schedule_flag_reads_plan_fixture(self):
        from opportunity_planner.cli import main

        fixture = self.write(
            'schedule.json',
            {
                'items': [
                    {
                        'id': 'plan-1',
                        'owner_id': 'person-1',
                        'completed_at': '2026-11-16T10:00:00+00:00',
                        'activities': [
                            {
                                'id': 'course',
                                'kind': 'training',
                                'start': '2026-11-09T09:00:00+00:00',
                                'end': '2026-11-16T09:00:00+00:00',
                                'location': 'school',
                                'capacity': 5,
                                'booked': 0,
                            },
                            {
                                'id': 'shift',
                                'kind': 'gig',
                                'start': '2026-11-16T11:00:00+00:00',
                                'end': '2026-11-16T13:00:00+00:00',
                                'location': 'job',
                                'capacity': 5,
                                'filled': 0,
                            },
                        ],
                    }
                ],
                'context': {'travel_matrix': {'school': {'job': 20}}, 'minimum_rest_minutes': 0},
            },
        )
        output = self.root / 'report.json'
        main(['reachability', '--fixture', fixture, '--output', str(output), '--schedule'])
        payload = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(payload['scenario'], 'schedule')
        self.assertEqual(payload['totals']['realized_arrangements'], 1)


if __name__ == '__main__':
    unittest.main()
