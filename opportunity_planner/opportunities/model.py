"""Normalized, read-only input model for opportunity reachability analysis.

The analyzer accepts plain mappings so advisers can feed it fixtures or API
payloads.  Everything is copied into immutable dataclasses here; the original
mapping is never touched.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from ..contracts import ContractError, fingerprint, require_datetime, require_identifier


class ReachabilityError(ValueError):
    """Raised when an analysis input cannot be interpreted safely."""


def _moment(value: Any, name: str) -> dt.datetime | None:
    if value is None or value == '':
        return None
    try:
        return require_datetime(value, name)
    except ContractError as exc:
        raise ReachabilityError(str(exc)) from exc


def _identifier(value: Any, name: str) -> str:
    try:
        return require_identifier(value, name)
    except ContractError as exc:
        raise ReachabilityError(str(exc)) from exc


def _integer(value: Any, name: str, default: int | None = None, minimum: int = 0) -> int | None:
    if value in (None, ''):
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReachabilityError(f'{name} must be integer')
    if value < minimum:
        raise ReachabilityError(f'{name} out of range')
    return value


def _strings(values: Any) -> tuple[str, ...]:
    if values in (None, ''):
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        raise ReachabilityError('prerequisites must be a list')
    result = []
    for item in values:
        text = ' '.join(str(item).split())
        if text:
            result.append(text)
    return tuple(sorted(set(result)))


def _attributes(value: Any) -> Mapping[str, Any]:
    if value in (None, ''):
        return {}
    if not isinstance(value, Mapping):
        raise ReachabilityError('attributes must be a mapping')
    return dict(value)


@dataclass(frozen=True)
class Course:
    course_id: str
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    location: str | None = None
    prerequisites: tuple[str, ...] = ()
    capacity: int | None = None
    enrolled: int | None = None
    status: str = 'active'


@dataclass(frozen=True)
class InterviewSlot:
    interview_id: str
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    window_start: dt.datetime | None = None
    window_end: dt.datetime | None = None
    location: str | None = None
    capacity: int | None = None
    booked: int | None = None
    prerequisites: tuple[str, ...] = ()
    status: str = 'active'

    @property
    def effective_start(self) -> dt.datetime | None:
        return self.start or self.window_start

    @property
    def effective_end(self) -> dt.datetime | None:
        return self.end or self.window_end

    @property
    def effective_window_start(self) -> dt.datetime | None:
        return self.window_start or self.start

    @property
    def effective_window_end(self) -> dt.datetime | None:
        return self.window_end or self.end


@dataclass(frozen=True)
class ShiftSlot:
    shift_id: str
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    location: str | None = None
    capacity: int | None = None
    filled: int | None = None
    open_from: dt.datetime | None = None
    open_until: dt.datetime | None = None
    status: str = 'active'


@dataclass(frozen=True)
class Person:
    person_id: str
    cohort_id: str | None = None
    course_id: str | None = None
    completed_at: dt.datetime | None = None
    completed_courses: frozenset[str] = frozenset()
    available_from: dt.datetime | None = None
    origin: str | None = None
    segment: str = 'unspecified'
    attributes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Cohort:
    cohort_id: str
    course_id: str | None = None
    label: str | None = None
    segment: str | None = None


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    kind: str = 'bundle'
    cohort_id: str | None = None
    course_id: str | None = None
    person_id: str | None = None
    interview: InterviewSlot | None = None
    shift: ShiftSlot | None = None
    prerequisites: tuple[str, ...] = ()

    @property
    def requires_interview(self) -> bool:
        return self.interview is not None

    @property
    def requires_shift(self) -> bool:
        return self.shift is not None


@dataclass(frozen=True)
class Commitment:
    commitment_id: str
    person_id: str | None
    start: dt.datetime
    end: dt.datetime
    kind: str = 'commitment'
    status: str = 'active'


@dataclass(frozen=True)
class Model:
    scenario: str
    as_of: dt.datetime | None
    minimum_rest_minutes: int
    travel_matrix: Mapping[str, Mapping[str, int | None]]
    people: tuple[Person, ...]
    cohorts: tuple[Cohort, ...]
    courses: Mapping[str, Course]
    interviews: Mapping[str, InterviewSlot]
    shifts: Mapping[str, ShiftSlot]
    opportunities: tuple[Opportunity, ...]
    commitments: tuple[Commitment, ...]
    input_fingerprint: str

    def cohort_for(self, person: Person) -> Cohort | None:
        cohort_id = person.cohort_id or person.course_id
        for cohort in self.cohorts:
            if cohort.cohort_id == cohort_id:
                return cohort
        return None

    def course_for(self, person: Person, opportunity: Opportunity | None = None) -> Course | None:
        course_id = None
        if opportunity is not None:
            course_id = opportunity.course_id
        if course_id is None:
            course_id = person.course_id
        if course_id is None:
            cohort = self.cohort_for(person)
            if cohort is not None:
                course_id = cohort.course_id
        if course_id is None:
            return None
        return self.courses.get(course_id)

    def commitments_for(self, person_id: str) -> tuple[Commitment, ...]:
        return tuple(
            item
            for item in self.commitments
            if item.person_id in (None, person_id) and item.status not in {'cancelled'}
        )


def safe_fingerprint(value: Any) -> str:
    """Fingerprint arbitrary JSON-ish data without mutating it."""

    try:
        return fingerprint(value)
    except (TypeError, ValueError):
        import hashlib
        import json

        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(',', ':'))
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _parse_course(record: Mapping[str, Any]) -> Course:
    course_id = _identifier(record.get('course_id') or record.get('id'), 'course_id')
    start = _moment(record.get('start') or record.get('effective_from'), f'{course_id}.start')
    end = _moment(record.get('end') or record.get('effective_to'), f'{course_id}.end')
    if start is not None and end is not None and end < start:
        raise ReachabilityError(f'{course_id} has reversed interval')
    return Course(
        course_id=course_id,
        start=start,
        end=end,
        location=str(record.get('location')).strip() if record.get('location') not in (None, '') else None,
        prerequisites=_strings(record.get('prerequisites')),
        capacity=_integer(record.get('capacity'), f'{course_id}.capacity'),
        enrolled=_integer(record.get('enrolled'), f'{course_id}.enrolled', 0),
        status=str(record.get('status', 'active')),
    )


def _parse_interview(record: Mapping[str, Any]) -> InterviewSlot:
    interview_id = _identifier(
        record.get('interview_id') or record.get('id'), 'interview_id'
    )
    start = _moment(record.get('start'), f'{interview_id}.start')
    end = _moment(record.get('end'), f'{interview_id}.end')
    window_start = _moment(record.get('window_start'), f'{interview_id}.window_start')
    window_end = _moment(record.get('window_end'), f'{interview_id}.window_end')
    if start is not None and end is not None and end < start:
        raise ReachabilityError(f'{interview_id} has reversed interval')
    if window_start is not None and window_end is not None and window_end < window_start:
        raise ReachabilityError(f'{interview_id} has reversed window')
    return InterviewSlot(
        interview_id=interview_id,
        start=start,
        end=end,
        window_start=window_start,
        window_end=window_end,
        location=str(record.get('location')).strip() if record.get('location') not in (None, '') else None,
        capacity=_integer(record.get('capacity'), f'{interview_id}.capacity'),
        booked=_integer(record.get('booked'), f'{interview_id}.booked', 0),
        prerequisites=_strings(record.get('prerequisites')),
        status=str(record.get('status', 'active')),
    )


def _parse_shift(record: Mapping[str, Any]) -> ShiftSlot:
    shift_id = _identifier(record.get('shift_id') or record.get('id'), 'shift_id')
    start = _moment(record.get('start'), f'{shift_id}.start')
    end = _moment(record.get('end'), f'{shift_id}.end')
    open_from = _moment(record.get('open_from'), f'{shift_id}.open_from')
    open_until = _moment(record.get('open_until'), f'{shift_id}.open_until')
    if start is not None and end is not None and end < start:
        raise ReachabilityError(f'{shift_id} has reversed interval')
    if open_from is not None and open_until is not None and open_until < open_from:
        raise ReachabilityError(f'{shift_id} has reversed window')
    return ShiftSlot(
        shift_id=shift_id,
        start=start,
        end=end,
        location=str(record.get('location')).strip() if record.get('location') not in (None, '') else None,
        capacity=_integer(record.get('capacity'), f'{shift_id}.capacity'),
        filled=_integer(record.get('filled'), f'{shift_id}.filled', 0),
        open_from=open_from,
        open_until=open_until,
        status=str(record.get('status', 'active')),
    )


def _parse_person(record: Mapping[str, Any]) -> Person:
    person_id = _identifier(record.get('person_id') or record.get('id'), 'person_id')
    cohort_id = record.get('cohort_id')
    course_id = record.get('course_id')
    completed_at = _moment(record.get('completed_at'), f'{person_id}.completed_at')
    available_from = _moment(record.get('available_from'), f'{person_id}.available_from')
    return Person(
        person_id=person_id,
        cohort_id=_identifier(cohort_id, f'{person_id}.cohort_id') if cohort_id else None,
        course_id=_identifier(course_id, f'{person_id}.course_id') if course_id else None,
        completed_at=completed_at,
        completed_courses=frozenset(_strings(record.get('completed_courses'))),
        available_from=available_from,
        origin=str(record.get('origin')).strip() if record.get('origin') not in (None, '') else None,
        segment=str(record.get('segment') or record.get('population') or 'unspecified').strip(),
        attributes=_attributes(record.get('attributes')),
    )


def _parse_cohort(record: Mapping[str, Any]) -> Cohort:
    cohort_id = _identifier(record.get('cohort_id') or record.get('id'), 'cohort_id')
    course_id = record.get('course_id')
    return Cohort(
        cohort_id=cohort_id,
        course_id=_identifier(course_id, f'{cohort_id}.course_id') if course_id else None,
        label=str(record.get('label')).strip() if record.get('label') not in (None, '') else None,
        segment=str(record.get('segment')).strip() if record.get('segment') not in (None, '') else None,
    )


def _parse_commitment(record: Mapping[str, Any]) -> Commitment:
    commitment_id = _identifier(
        record.get('commitment_id') or record.get('id'), 'commitment_id'
    )
    start = _moment(record.get('start'), f'{commitment_id}.start')
    end = _moment(record.get('end'), f'{commitment_id}.end')
    if start is None or end is None:
        raise ReachabilityError(f'{commitment_id} needs start and end')
    if end < start:
        raise ReachabilityError(f'{commitment_id} has reversed interval')
    person_id = record.get('person_id')
    return Commitment(
        commitment_id=commitment_id,
        person_id=_identifier(person_id, f'{commitment_id}.person_id') if person_id else None,
        start=start,
        end=end,
        kind=str(record.get('kind', 'commitment')),
        status=str(record.get('status', 'active')),
    )


def _parse_matrix(value: Any) -> dict[str, dict[str, int | None]]:
    if value in (None, ''):
        return {}
    if not isinstance(value, Mapping):
        raise ReachabilityError('travel_matrix must be a mapping')
    result: dict[str, dict[str, int | None]] = {}
    for source, targets in value.items():
        if not isinstance(targets, Mapping):
            raise ReachabilityError('travel_matrix rows must be mappings')
        row: dict[str, int | None] = {}
        for target, minutes in targets.items():
            if minutes in (None, ''):
                row[str(target)] = None
                continue
            try:
                number = int(minutes)
            except (TypeError, ValueError):
                row[str(target)] = None
                continue
            row[str(target)] = number if number >= 0 else None
        result[str(source)] = row
    return result


def _nested(record: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = record.get(key)
    return value if isinstance(value, Mapping) else None


def _flat_slot(record: Mapping[str, Any], prefix: str) -> dict[str, Any] | None:
    if not any(name in record for name in (f'{prefix}_id', f'{prefix}_start', f'{prefix}_end')):
        return None
    return {
        'id': record.get(f'{prefix}_id'),
        'start': record.get(f'{prefix}_start'),
        'end': record.get(f'{prefix}_end'),
        'location': record.get(f'{prefix}_location'),
        'capacity': record.get(f'{prefix}_capacity'),
        'booked': record.get(f'{prefix}_booked'),
        'filled': record.get(f'{prefix}_filled'),
    }


def _parse_opportunity(
    record: Mapping[str, Any],
    courses: Mapping[str, Course],
    interviews: Mapping[str, InterviewSlot],
    shifts: Mapping[str, ShiftSlot],
) -> Opportunity:
    # An explicit opportunity may reference existing interviews/shifts by id.
    interview_record = _nested(record, 'interview') or _flat_slot(record, 'interview')
    shift_record = _nested(record, 'shift') or _flat_slot(record, 'shift')
    interview_id = record.get('interview_id')
    shift_id = record.get('shift_id')

    interview: InterviewSlot | None = None
    if interview_record is not None:
        interview = _parse_interview(interview_record)
    elif interview_id and str(interview_id) in interviews:
        interview = interviews[str(interview_id)]

    shift: ShiftSlot | None = None
    if shift_record is not None:
        shift = _parse_shift(shift_record)
    elif shift_id and str(shift_id) in shifts:
        shift = shifts[str(shift_id)]

    if interview is None and shift is None:
        raise ReachabilityError('opportunity needs an interview or a shift')

    opportunity_id = _identifier(
        record.get('opportunity_id') or record.get('id') or f'opp-{interview.interview_id if interview else ""}-{shift.shift_id if shift else ""}',
        'opportunity_id',
    )
    kind = str(record.get('kind') or ('bundle' if interview and shift else 'interview' if interview else 'gig'))
    cohort_id = record.get('cohort_id')
    course_id = record.get('course_id')
    person_id = record.get('person_id')
    return Opportunity(
        opportunity_id=opportunity_id,
        kind=kind,
        cohort_id=_identifier(cohort_id, f'{opportunity_id}.cohort_id') if cohort_id else None,
        course_id=_identifier(course_id, f'{opportunity_id}.course_id') if course_id else None,
        person_id=_identifier(person_id, f'{opportunity_id}.person_id') if person_id else None,
        interview=interview,
        shift=shift,
        prerequisites=_strings(record.get('prerequisites')),
    )


def _derived_opportunities(
    interviews: Mapping[str, InterviewSlot],
    shifts: Mapping[str, ShiftSlot],
    mode: str = 'chain',
) -> tuple[Opportunity, ...]:
    """Enumerate the interview -> shift chains that can be examined.

    ``mode='chain'`` (default when interviews and shifts both exist) tests the
    full course -> interview -> gig chain the adviser is promising.  With
    ``mode='independent'`` the interview and the gig are separate opportunities,
    so a person who cannot make the interview can still be counted for a gig
    they can actually reach.  Direction and timing are *not* assumed here: the
    evaluator tests them and reports blocked paths when the matrix or the window
    is missing.
    """

    if mode not in {'chain', 'independent'}:
        raise ReachabilityError('opportunity_mode must be chain or independent')
    if mode == 'chain' and interviews and shifts:
        result = [
            Opportunity(
                opportunity_id=f'opp-{interview.interview_id}-{shift.shift_id}',
                kind='bundle',
                interview=interview,
                shift=shift,
            )
            for interview in interviews.values()
            for shift in shifts.values()
        ]
    else:
        result = [
            Opportunity(
                opportunity_id=f'opp-{interview.interview_id}',
                kind='interview',
                interview=interview,
            )
            for interview in interviews.values()
        ]
        result += [
            Opportunity(
                opportunity_id=f'opp-{shift.shift_id}',
                kind='gig',
                shift=shift,
            )
            for shift in shifts.values()
        ]
    return tuple(sorted(result, key=lambda item: item.opportunity_id))


def build_model(data: Mapping[str, Any]) -> Model:
    if not isinstance(data, Mapping):
        raise ReachabilityError('analysis input must be a mapping')

    as_of = _moment(data.get('as_of'), 'as_of')
    minimum_rest = _integer(data.get('minimum_rest_minutes'), 'minimum_rest_minutes', 0) or 0
    travel_matrix = _parse_matrix(data.get('travel_matrix'))
    scenario = str(data.get('scenario') or data.get('label') or 'scenario')

    raw_people: Sequence[Mapping[str, Any]] = data.get('people') or data.get('persons') or ()
    if not raw_people:
        raise ReachabilityError('at least one person is required')
    people = tuple(sorted((_parse_person(item) for item in raw_people), key=lambda item: item.person_id))

    courses = {
        course.course_id: course
        for course in (_parse_course(item) for item in (data.get('courses') or ()))
    }
    interviews = {
        slot.interview_id: slot
        for slot in (_parse_interview(item) for item in (data.get('interviews') or ()))
    }
    shifts = {
        slot.shift_id: slot
        for slot in (_parse_shift(item) for item in (data.get('shifts') or ()))
    }

    raw_cohorts = data.get('cohorts') or ()
    cohorts = tuple(sorted((_parse_cohort(item) for item in raw_cohorts), key=lambda item: item.cohort_id))
    if not cohorts:
        derived = {}
        for person in people:
            cohort_id = person.cohort_id or person.course_id or 'unassigned'
            derived.setdefault(cohort_id, Cohort(cohort_id=cohort_id, course_id=person.course_id))
        cohorts = tuple(sorted(derived.values(), key=lambda item: item.cohort_id))

    raw_opportunities = data.get('opportunities') or ()
    if raw_opportunities:
        opportunities = tuple(
            sorted(
                (
                    _parse_opportunity(item, courses, interviews, shifts)
                    for item in raw_opportunities
                ),
                key=lambda item: item.opportunity_id,
            )
        )
    else:
        opportunities = _derived_opportunities(
            interviews, shifts, str(data.get('opportunity_mode') or 'chain')
        )

    commitments = tuple(
        sorted(
            (_parse_commitment(item) for item in (data.get('commitments') or ())),
            key=lambda item: (item.start, item.end, item.commitment_id),
        )
    )

    commit_ids = {item.commitment_id for item in commitments}
    if len(commit_ids) != len(commitments):
        raise ReachabilityError('duplicate commitment ids')

    return Model(
        scenario=scenario,
        as_of=as_of,
        minimum_rest_minutes=minimum_rest,
        travel_matrix=travel_matrix,
        people=people,
        cohorts=cohorts,
        courses=courses,
        interviews=interviews,
        shifts=shifts,
        opportunities=opportunities,
        commitments=commitments,
        input_fingerprint=safe_fingerprint(data),
    )
