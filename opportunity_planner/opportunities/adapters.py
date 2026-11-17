"""Adapters from existing schedule/commitment payloads to the reachability model.

The existing scheduler accepts a plan with ``activities`` (training, interview,
gig) and a context holding ``travel_matrix``, ``completed_courses`` and rest
buffers.  Advisers already build those payloads; this module lets the same
inputs be analysed for downstream reachability without re-keying anything.
The adapter only reads its input and returns plain dictionaries.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, Sequence

from .model import ReachabilityError


def _activities(item: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    value = item.get('activities') or ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ReachabilityError('activities must be a list')
    return value


def _kind(activity: Mapping[str, Any]) -> str:
    kind = str(activity.get('kind') or '').strip().lower()
    if kind in {'training', 'course', 'class', 'lesson'}:
        return 'course'
    if kind in {'interview', 'screening'}:
        return 'interview'
    if kind in {'gig', 'shift', 'work', 'job'}:
        return 'shift'
    return kind or 'activity'


def _completed_courses(item: Mapping[str, Any], context: Mapping[str, Any]) -> list[str]:
    completed = list(context.get('completed_courses') or ())
    for activity in _activities(item):
        if _kind(activity) == 'course' and activity.get('completed') is True:
            identifier = activity.get('id')
            if identifier:
                completed.append(str(identifier))
    return sorted(set(completed))


def from_schedule(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Convert one schedule fixture into reachability input.

    ``payload`` may be a whole fixture (``items`` + ``context``) or a single
    plan item with a sibling ``context``.
    """

    if not isinstance(payload, Mapping):
        raise ReachabilityError('schedule payload must be a mapping')
    working = copy.deepcopy(payload)
    context = working.get('context') or {}
    items = working.get('items')
    if items is None:
        items = [working]
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ReachabilityError('items must be a list')

    courses: dict[str, dict[str, Any]] = {}
    interviews: dict[str, dict[str, Any]] = {}
    shifts: dict[str, dict[str, Any]] = {}
    people: list[dict[str, Any]] = []
    commitments: list[dict[str, Any]] = []
    cohort_id = str(working.get('scenario') or working.get('cohort_id') or 'schedule')

    for position, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        person_id = str(item.get('owner_id') or item.get('person_id') or item.get('id') or f'person-{position}')
        plan_course: Mapping[str, Any] | None = None
        for activity in _activities(item):
            kind = _kind(activity)
            identifier = str(activity.get('id') or '')
            if not identifier:
                raise ReachabilityError('every activity needs an id')
            if kind == 'course':
                plan_course = activity
                courses.setdefault(
                    identifier,
                    {
                        'course_id': identifier,
                        'start': activity.get('start'),
                        'end': activity.get('end'),
                        'location': activity.get('location'),
                        'prerequisites': list(activity.get('prerequisites') or ()),
                        'capacity': activity.get('capacity'),
                        'enrolled': activity.get('booked') or activity.get('enrolled') or 0,
                    },
                )
            elif kind == 'interview':
                interviews.setdefault(
                    identifier,
                    {
                        'interview_id': identifier,
                        'start': activity.get('start'),
                        'end': activity.get('end'),
                        'window_start': activity.get('window_start'),
                        'window_end': activity.get('window_end'),
                        'location': activity.get('location'),
                        'capacity': activity.get('capacity'),
                        'booked': activity.get('booked'),
                        'prerequisites': list(activity.get('prerequisites') or ()),
                    },
                )
            elif kind == 'shift':
                shifts.setdefault(
                    identifier,
                    {
                        'shift_id': identifier,
                        'start': activity.get('start'),
                        'end': activity.get('end'),
                        'location': activity.get('location'),
                        'capacity': activity.get('capacity'),
                        'filled': activity.get('filled') or activity.get('booked') or 0,
                        'open_from': activity.get('open_from'),
                        'open_until': activity.get('open_until'),
                    },
                )
        if item.get('completed_at'):
            completed_at = str(item['completed_at'])
        elif plan_course is not None and plan_course.get('completed_at'):
            completed_at = str(plan_course['completed_at'])
        else:
            completed_at = None
        people.append(
            {
                'person_id': person_id,
                'cohort_id': str(item.get('cohort_id') or cohort_id),
                'course_id': plan_course.get('id') if plan_course is not None else item.get('course_id'),
                'completed_at': completed_at,
                'completed_courses': _completed_courses(item, context),
                'available_from': item.get('available_from'),
                'origin': item.get('origin') or (plan_course.get('location') if plan_course else None),
                'segment': item.get('segment') or item.get('population') or 'unspecified',
            }
        )
        for index, booking in enumerate(context.get('bookings') or ()):
            if not isinstance(booking, Mapping):
                continue
            person = booking.get('person_id') or person_id
            if person != person_id:
                continue
            commitments.append(
                {
                    'commitment_id': str(booking.get('commitment_id') or booking.get('id') or f'{person_id}-booking-{index}'),
                    'person_id': person_id,
                    'start': booking.get('start'),
                    'end': booking.get('end'),
                    'kind': booking.get('kind', 'booking'),
                }
            )

    result: dict[str, Any] = {
        'scenario': cohort_id,
        'as_of': working.get('as_of') or context.get('as_of'),
        'minimum_rest_minutes': working.get('minimum_rest_minutes')
        or context.get('minimum_rest_minutes', 0),
        'travel_matrix': context.get('travel_matrix') or working.get('travel_matrix') or {},
        'courses': list(courses.values()),
        'interviews': list(interviews.values()),
        'shifts': list(shifts.values()),
        'people': people,
        'commitments': commitments,
    }
    cohorts = working.get('cohorts')
    if cohorts:
        result['cohorts'] = cohorts
    opportunities = working.get('opportunities')
    if opportunities:
        result['opportunities'] = opportunities
    return result


def _attributes(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get('attributes') or {}
    return dict(value) if isinstance(value, Mapping) else {}


def from_records(
    courses: Sequence[Mapping[str, Any]] = (),
    interviews: Sequence[Mapping[str, Any]] = (),
    shifts: Sequence[Mapping[str, Any]] = (),
    persons: Sequence[Mapping[str, Any]] = (),
    bookings: Sequence[Mapping[str, Any]] = (),
    plans: Sequence[Mapping[str, Any]] = (),
    scenario: str | None = None,
) -> dict[str, Any]:
    """Build reachability input from stored scheduling records.

    Only reads the supplied rows.  The travel matrix, rest buffer, cohort
    segmentation and completion evidence are taken from record ``attributes``;
    nothing is inferred from quantity or amount because those are commercial
    fields, not itinerary facts.
    """

    course_rows: list[dict[str, Any]] = []
    for row in courses:
        attributes = _attributes(row)
        course_rows.append(
            {
                'course_id': str(row.get('course_id') or row.get('id')),
                'start': attributes.get('start') or row.get('effective_from'),
                'end': attributes.get('end') or row.get('effective_to'),
                'location': attributes.get('location'),
                'prerequisites': attributes.get('prerequisites') or (),
                'capacity': attributes.get('capacity'),
                'enrolled': attributes.get('enrolled', row.get('quantity', 0)),
            }
        )

    interview_rows: list[dict[str, Any]] = []
    for row in interviews:
        attributes = _attributes(row)
        interview_rows.append(
            {
                'interview_id': str(row.get('interview_id') or row.get('id')),
                'start': attributes.get('start') or row.get('effective_from'),
                'end': attributes.get('end') or row.get('effective_to'),
                'window_start': attributes.get('window_start'),
                'window_end': attributes.get('window_end'),
                'location': attributes.get('location'),
                'capacity': attributes.get('capacity'),
                'booked': attributes.get('booked', row.get('quantity', 0)),
                'prerequisites': attributes.get('prerequisites') or (),
            }
        )

    shift_rows: list[dict[str, Any]] = []
    for row in shifts:
        attributes = _attributes(row)
        shift_rows.append(
            {
                'shift_id': str(row.get('shift_id') or row.get('id')),
                'start': attributes.get('start') or row.get('effective_from'),
                'end': attributes.get('end') or row.get('effective_to'),
                'location': attributes.get('location'),
                'capacity': attributes.get('capacity'),
                'filled': attributes.get('filled', row.get('quantity', 0)),
                'open_from': attributes.get('open_from'),
                'open_until': attributes.get('open_until'),
            }
        )

    people_rows: list[dict[str, Any]] = []
    for row in persons:
        attributes = _attributes(row)
        people_rows.append(
            {
                'person_id': str(row.get('person_id') or row.get('id')),
                'cohort_id': attributes.get('cohort_id'),
                'course_id': attributes.get('course_id'),
                'completed_at': attributes.get('completed_at'),
                'completed_courses': attributes.get('completed_courses') or (),
                'available_from': attributes.get('available_from'),
                'origin': attributes.get('origin'),
                'segment': attributes.get('segment') or row.get('category') or 'unspecified',
            }
        )

    commitment_rows: list[dict[str, Any]] = []
    for index, row in enumerate(bookings):
        attributes = _attributes(row)
        commitment_rows.append(
            {
                'commitment_id': str(row.get('booking_id') or row.get('id') or f'booking-{index}'),
                'person_id': attributes.get('person_id') or row.get('owner_id'),
                'start': attributes.get('start') or row.get('effective_from'),
                'end': attributes.get('end') or row.get('effective_to'),
                'kind': attributes.get('kind') or row.get('category') or 'booking',
            }
        )

    travel_matrix: dict[str, Any] = {}
    minimum_rest = 0
    as_of = None
    for row in plans:
        attributes = _attributes(row)
        if attributes.get('travel_matrix'):
            travel_matrix = attributes['travel_matrix']
        if attributes.get('minimum_rest_minutes') is not None:
            minimum_rest = attributes['minimum_rest_minutes']
        if attributes.get('as_of') is not None:
            as_of = attributes['as_of']

    result: dict[str, Any] = {
        'scenario': scenario or 'stored-schedule',
        'as_of': as_of,
        'minimum_rest_minutes': minimum_rest,
        'travel_matrix': travel_matrix,
        'courses': course_rows,
        'interviews': interview_rows,
        'shifts': shift_rows,
        'people': people_rows,
        'commitments': commitment_rows,
    }
    return result


def from_database(database: Any, scenario: str | None = None) -> dict[str, Any]:
    """Read scheduling records from :class:`opportunity_planner.storage.Database`.

    Uses only ``list_records``; the analysis itself never writes.
    """

    return from_records(
        courses=database.list_records('course'),
        interviews=database.list_records('interview'),
        shifts=database.list_records('shift'),
        persons=database.list_records('person'),
        bookings=database.list_records('booking'),
        plans=database.list_records('plan'),
        scenario=scenario,
    )
