"""Read-only reachability evaluation and reporting.

Every opportunity is evaluated as a chain::

    course completion -> travel -> interview window -> travel -> shift quota

The evaluator never mutates its input, never writes to storage and never
cancels or rebooks anything.  See :mod:`opportunity_planner.opportunities.model`
for the normalized inputs.
"""

from __future__ import annotations

import copy
import datetime as dt
import decimal
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Sequence

from .model import (
    Commitment,
    Model,
    Opportunity,
    Person,
    ReachabilityError,
    build_model,
    safe_fingerprint,
)

EPOCH = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


class ReachStatus(str, Enum):
    FULFILLED = 'fulfilled'
    THEORETICAL = 'theoretical'
    INVALID = 'invalid'
    BLOCKED = 'blocked'


STATUS_ORDER = (
    ReachStatus.FULFILLED.value,
    ReachStatus.THEORETICAL.value,
    ReachStatus.BLOCKED.value,
    ReachStatus.INVALID.value,
)

BLOCKER_CATEGORIES = {
    'timing': 'the course finish, arrival or window does not line up',
    'capacity': 'a place was already taken or the quota is unknown',
    'prerequisite': 'a required course or credential is missing',
    'availability': 'the person already has a commitment at that time',
    'evidence': 'completion is not evidenced, so this is only theoretical',
    'data': 'a key itinerary fact is missing and cannot be assumed',
}


def blocker_category(reason: str) -> str:
    """Classify a reason code so advisers can separate time from quota loss."""

    if reason.startswith('travel_unknown') or reason.endswith('_location_unknown') or reason in {
        'interview_window_missing',
        'shift_window_missing',
        'course_missing',
        'completion_time_unknown',
    }:
        return 'data'
    if reason.startswith('course_prerequisite') or reason.startswith('interview_prerequisite') or reason.startswith(
        'opportunity_prerequisite'
    ):
        return 'prerequisite'
    if reason.startswith('commitment_conflict'):
        return 'availability'
    if reason == 'course_not_completed':
        return 'evidence'
    if 'capacity' in reason:
        return 'capacity'
    if 'window' in reason or 'arrival' in reason or 'open' in reason or 'interval' in reason:
        return 'timing'
    return 'data'


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _rate(numerator: int, denominator: int) -> str | None:
    if denominator <= 0:
        return None
    return format(
        (decimal.Decimal(numerator) / decimal.Decimal(denominator)).quantize(
            decimal.Decimal('0.0001')
        ),
        'f',
    )


def _overlap(start: dt.datetime, end: dt.datetime, other_start: dt.datetime, other_end: dt.datetime) -> bool:
    return start < other_end and other_start < end


@dataclass(frozen=True)
class Answer:
    """A single evaluated person/opportunity chain."""

    person_id: str
    cohort_id: str | None
    course_id: str | None
    segment: str
    opportunity_id: str
    kind: str
    completed: bool
    completed_at: dt.datetime | None
    individual_status: str
    status: str
    counted: bool
    reasons: tuple[str, ...]
    details: Mapping[str, str]
    path: tuple[Mapping[str, Any], ...]
    failed_step: str | None
    interview_id: str | None
    shift_id: str | None
    capacity: Mapping[str, Mapping[str, int | None]]

    def as_dict(self) -> dict[str, Any]:
        return {
            'person_id': self.person_id,
            'cohort_id': self.cohort_id,
            'course_id': self.course_id,
            'segment': self.segment,
            'opportunity_id': self.opportunity_id,
            'kind': self.kind,
            'completed': self.completed,
            'completed_at': _iso(self.completed_at),
            'individual_status': self.individual_status,
            'status': self.status,
            'counted': self.counted,
            'reasons': list(self.reasons),
            'details': dict(self.details),
            'path': [dict(step) for step in self.path],
            'failed_step': self.failed_step,
            'interview_id': self.interview_id,
            'shift_id': self.shift_id,
            'capacity': {key: dict(value) for key, value in self.capacity.items()},
        }


def _is_capacity_lost(link: Answer) -> bool:
    return 'capacity_allocated_elsewhere' in link.reasons


def _is_suppressed(link: Answer, counted_persons: set[str]) -> bool:
    """A feasible duplicate is suppressed only if that person is already counted."""

    return link.individual_status == ReachStatus.FULFILLED.value and not link.counted and not _is_capacity_lost(link) and link.person_id in counted_persons


@dataclass
class ReachabilityReport:
    scenario: str
    as_of: dt.datetime | None
    input_fingerprint: str
    fingerprint_after: str
    inputs_unchanged: bool
    allocation_policy: str
    persons: tuple[dict[str, Any], ...]
    links: tuple[Answer, ...]
    excluded_people: tuple[dict[str, Any], ...]
    totals: Mapping[str, Any]
    by_course: Mapping[str, Any]
    by_cohort: Mapping[str, Any]
    by_segment: Mapping[str, Any]
    by_course_segment: Mapping[str, Any]
    capacity_pressure: tuple[dict[str, Any], ...]
    capacity_allocation: tuple[dict[str, Any], ...]
    notes: tuple[str, ...] = ()

    def _counted_persons(self) -> set[str]:
        return {link.person_id for link in self.links if link.counted}

    @property
    def blocked_links(self) -> tuple[Answer, ...]:
        counted = self._counted_persons()
        return tuple(
            link
            for link in self.links
            if not link.counted and not _is_suppressed(link, counted)
        )

    @property
    def fulfilled_links(self) -> tuple[Answer, ...]:
        return tuple(link for link in self.links if link.counted)

    @property
    def suppressed_links(self) -> tuple[Answer, ...]:
        counted = self._counted_persons()
        return tuple(link for link in self.links if _is_suppressed(link, counted))

    @property
    def capacity_lost_links(self) -> tuple[Answer, ...]:
        return tuple(link for link in self.links if _is_capacity_lost(link))

    def as_dict(self) -> dict[str, Any]:
        return {
            'scenario': self.scenario,
            'as_of': _iso(self.as_of),
            'read_only': {
                'inputs_unchanged': self.inputs_unchanged,
                'input_fingerprint': self.input_fingerprint,
                'fingerprint_after': self.fingerprint_after,
                'storage_writes': 0,
            },
            'allocation_policy': self.allocation_policy,
            'totals': dict(self.totals),
            'by_course': dict(self.by_course),
            'by_cohort': dict(self.by_cohort),
            'by_segment': dict(self.by_segment),
            'by_course_segment': {
                course: dict(segments) for course, segments in self.by_course_segment.items()
            },
            'persons': [dict(item) for item in self.persons],
            'links': [link.as_dict() for link in self.links],
            'fulfilled_links': [link.as_dict() for link in self.fulfilled_links],
            'blocked_paths': [link.as_dict() for link in self.blocked_links],
            'suppressed_links': [link.as_dict() for link in self.suppressed_links],
            'capacity_lost_links': [link.as_dict() for link in self.capacity_lost_links],
            'capacity_pressure': [dict(item) for item in self.capacity_pressure],
            'capacity_allocation': [dict(item) for item in self.capacity_allocation],
            'excluded_people': [dict(item) for item in self.excluded_people],
            'notes': list(self.notes),
        }


class _Builder:
    """Builds one answer for one person/opportunity pair."""

    def __init__(self, model: Model, person: Person, opportunity: Opportunity):
        self.model = model
        self.person = person
        self.opportunity = opportunity
        self.completed = person.completed_at is not None
        self.completion_at = person.completed_at
        self.invalid: list[str] = []
        self.blocked: list[str] = []
        self.details: dict[str, str] = {}
        self.path: list[dict[str, Any]] = []
        self.failed_step: str | None = None
        self.capacity: dict[str, dict[str, int | None]] = {}
        self.earned = set(person.completed_courses)
        cohort = model.cohort_for(person)
        self.cohort_id = person.cohort_id or (cohort.cohort_id if cohort else None)
        self.course = model.course_for(person, opportunity)
        self.course_id = (
            opportunity.course_id
            or person.course_id
            or (cohort.course_id if cohort is not None else None)
            or (self.course.course_id if self.course is not None else None)
        )
        if self.completed and self.course_id is not None:
            self.earned.add(self.course_id)

    # -- issue helpers -------------------------------------------------
    def _invalid(self, code: str, detail: str, step: str) -> None:
        if code not in self.invalid:
            self.invalid.append(code)
        self.details.setdefault(code, detail)
        if self.failed_step is None:
            self.failed_step = step

    def _blocked(self, code: str, detail: str, step: str) -> None:
        if code not in self.blocked:
            self.blocked.append(code)
        self.details.setdefault(code, detail)
        if self.failed_step is None and not self.invalid:
            self.failed_step = step

    def _step(self, **values: Any) -> None:
        self.path.append(dict(values))

    def _travel(self, source: str, target: str, step: str) -> int | None:
        if source == target:
            # No journey is implied when both ends share a location.
            self._step(step='travel', leg=step, **{'from': source, 'to': target, 'minutes': 0, 'known': True})
            return 0
        row = self.model.travel_matrix.get(source)
        if row is None or target not in row or row[target] is None:
            code = f'travel_unknown:{source}->{target}'
            self._blocked(
                code,
                f'no usable travel time recorded from {source} to {target}',
                step,
            )
            self._step(step='travel', leg=step, **{'from': source, 'to': target, 'minutes': None, 'known': False})
            return None
        minutes = row[target]
        self._step(step='travel', leg=step, **{'from': source, 'to': target, 'minutes': minutes, 'known': True})
        return minutes

    def _commitments(self) -> tuple[Commitment, ...]:
        return self.model.commitments_for(self.person.person_id)

    def _origin(self) -> str | None:
        if self.course is not None and self.course.location:
            return self.course.location
        return self.person.origin

    # -- chains --------------------------------------------------------
    def _course_leg(self) -> dt.datetime | None:
        course = self.course
        if course is None:
            self._blocked('course_missing', 'no course is assigned to this person', 'course')
            self._step(step='course', ref=None, known=False)
            return None
        self._step(
            step='course',
            ref=course.course_id,
            start=_iso(course.start),
            end=_iso(course.end),
            location=course.location,
        )
        if course.status == 'cancelled':
            self._invalid('course_cancelled', f'{course.course_id} is cancelled', 'course')
        if course.start is not None and course.end is not None and course.end < course.start:
            self._invalid('course_interval_invalid', f'{course.course_id} ends before it starts', 'course')
        if not self.completed and course.end is None:
            self._blocked(
                'completion_time_unknown',
                f'{course.course_id} has no end time to estimate completion',
                'course',
            )
        if self._origin() is None:
            self._blocked(
                'course_location_unknown',
                f'{course.course_id} has no location and the person has no origin',
                'course_location',
            )

        missing = tuple(sorted(set(course.prerequisites) - (self.earned - {course.course_id})))
        if missing:
            self._invalid(
                'course_prerequisite_missing:' + ','.join(missing),
                f'missing course prerequisites: {", ".join(missing)}',
                'course_prerequisite',
            )

        for commitment in self._commitments():
            if commitment.kind in {'course', 'training'}:
                continue
            if course.start is not None and course.end is not None and _overlap(
                course.start, course.end, commitment.start, commitment.end
            ):
                self._invalid(
                    f'commitment_conflict:{commitment.commitment_id}',
                    f'course overlaps commitment {commitment.commitment_id}',
                    'course_commitment',
                )

        if not self.completed:
            if course.capacity is None or course.enrolled is None:
                self._blocked(
                    'course_capacity_unknown',
                    f'{course.course_id} does not report capacity and enrolment',
                    'course_capacity',
                )
            elif course.enrolled >= course.capacity:
                self._invalid(
                    'course_capacity_exhausted',
                    f'{course.course_id} is full ({course.enrolled}/{course.capacity})',
                    'course_capacity',
                )
            self._step(step='completion', completed=False, at=None, evidenced_by='none')
            return course.end

        self._step(
            step='completion',
            completed=True,
            at=_iso(self.completion_at),
            evidenced_by='completed_at',
        )
        # Completion evidence is trusted as given: someone who finished an
        # earlier offering of the same course is ready earlier, not later.
        # The comparison view shows that such people are unaffected by moving
        # this course's end date.
        ready: dt.datetime | None = self.completion_at
        if self.person.available_from is not None and self.person.available_from > ready:
            ready = self.person.available_from
        self._step(step='ready', at=_iso(ready))
        return ready

    def _interview_leg(self, ready: dt.datetime | None) -> tuple[dt.datetime | None, dt.datetime | None]:
        slot = self.opportunity.interview
        assert slot is not None
        start = slot.effective_start
        end = slot.effective_end
        self._step(
            step='interview',
            ref=slot.interview_id,
            start=_iso(start),
            end=_iso(end),
            window_start=_iso(slot.effective_window_start),
            window_end=_iso(slot.effective_window_end),
            location=slot.location,
        )
        missing = tuple(sorted(set(slot.prerequisites) - self.earned))
        if missing:
            self._invalid(
                'interview_prerequisite_missing:' + ','.join(missing),
                f'missing interview prerequisites: {", ".join(missing)}',
                'interview_prerequisite',
            )
        if start is None or end is None:
            self._blocked(
                'interview_window_missing',
                f'{slot.interview_id} has no complete interview window',
                'interview_window',
            )
            return None, None
        if end <= start:
            self._invalid(
                'interview_interval_invalid',
                f'{slot.interview_id} has a non-positive slot',
                'interview_window',
            )
        if slot.window_start is not None and start < slot.window_start:
            self._invalid(
                'interview_outside_window',
                f'{slot.interview_id} starts before its window opens',
                'interview_window',
            )
        if slot.window_end is not None and end > slot.window_end:
            self._invalid(
                'interview_outside_window',
                f'{slot.interview_id} ends after its window closes',
                'interview_window',
            )
        if slot.location is None:
            self._blocked('interview_location_unknown', f'{slot.interview_id} has no location', 'interview_location')
        if slot.capacity is None or slot.booked is None:
            self.capacity['interview'] = {'capacity': slot.capacity, 'booked': slot.booked, 'remaining': None}
            self._blocked(
                'interview_capacity_unknown',
                f'{slot.interview_id} does not report capacity and bookings',
                'interview_capacity',
            )
        else:
            remaining = max(0, slot.capacity - slot.booked)
            self.capacity['interview'] = {
                'capacity': slot.capacity,
                'booked': slot.booked,
                'remaining': remaining,
            }
            if remaining == 0:
                self._invalid(
                    'interview_capacity_exhausted',
                    f'{slot.interview_id} is full ({slot.booked}/{slot.capacity})',
                    'interview_capacity',
                )
        origin = self._origin()
        if ready is not None and origin is not None and slot.location is not None:
            minutes = self._travel(origin, slot.location, 'travel_course_interview')
            if minutes is not None:
                arrival = ready + dt.timedelta(minutes=minutes + self.model.minimum_rest_minutes)
                self._step(step='arrival', ref=slot.interview_id, at=_iso(arrival))
                if arrival > start:
                    self._invalid(
                        'interview_window_missed',
                        f'arrival {_iso(arrival)} is after interview start {_iso(start)}',
                        'interview_window',
                    )
        for commitment in self._commitments():
            if _overlap(start, end, commitment.start, commitment.end):
                self._invalid(
                    f'commitment_conflict:{commitment.commitment_id}',
                    f'interview {slot.interview_id} overlaps commitment {commitment.commitment_id}',
                    'interview_commitment',
                )
        return start, end

    def _shift_leg(self, ready: dt.datetime | None, interview_end: dt.datetime | None) -> None:
        slot = self.opportunity.shift
        assert slot is not None
        self._step(
            step='shift',
            ref=slot.shift_id,
            start=_iso(slot.start),
            end=_iso(slot.end),
            open_from=_iso(slot.open_from),
            open_until=_iso(slot.open_until),
            location=slot.location,
        )
        if slot.start is None or slot.end is None:
            self._blocked('shift_window_missing', f'{slot.shift_id} has no complete shift window', 'shift_window')
            return
        if slot.end < slot.start:
            self._invalid('shift_interval_invalid', f'{slot.shift_id} ends before it starts', 'shift_window')
        if slot.location is None:
            self._blocked('shift_location_unknown', f'{slot.shift_id} has no location', 'shift_location')
        if slot.capacity is None or slot.filled is None:
            self.capacity['shift'] = {'capacity': slot.capacity, 'filled': slot.filled, 'remaining': None}
            self._blocked(
                'shift_capacity_unknown',
                f'{slot.shift_id} does not report capacity and filled places',
                'shift_capacity',
            )
        else:
            remaining = max(0, slot.capacity - slot.filled)
            self.capacity['shift'] = {
                'capacity': slot.capacity,
                'filled': slot.filled,
                'remaining': remaining,
            }
            if remaining == 0:
                self._invalid(
                    'shift_capacity_exhausted',
                    f'{slot.shift_id} has no places left ({slot.filled}/{slot.capacity})',
                    'shift_capacity',
                )
        if slot.open_from is not None and slot.start < slot.open_from:
            self._invalid('shift_before_open', f'{slot.shift_id} starts before it opens', 'shift_booking_window')
        if slot.open_until is not None and slot.start > slot.open_until:
            self._invalid(
                'shift_booking_window_closed',
                f'{slot.shift_id} starts after its booking window closes',
                'shift_booking_window',
            )

        origin = None
        ready_at = None
        if self.opportunity.interview is not None:
            origin = self.opportunity.interview.location
            ready_at = interview_end
        else:
            origin = self._origin()
            ready_at = ready
        if origin is not None and slot.location is not None and ready_at is not None:
            step = 'travel_interview_shift' if self.opportunity.interview is not None else 'travel_course_shift'
            minutes = self._travel(origin, slot.location, step)
            if minutes is not None:
                arrival = ready_at + dt.timedelta(minutes=minutes + self.model.minimum_rest_minutes)
                self._step(step='arrival', ref=slot.shift_id, at=_iso(arrival))
                if arrival > slot.start:
                    self._invalid(
                        'shift_window_missed',
                        f'arrival {_iso(arrival)} is after shift start {_iso(slot.start)}',
                        'shift_window',
                    )
                if slot.open_until is not None and arrival > slot.open_until:
                    self._invalid(
                        'shift_booking_window_closed',
                        f'arrival {_iso(arrival)} is after booking closes {_iso(slot.open_until)}',
                        'shift_booking_window',
                    )
        for commitment in self._commitments():
            if _overlap(slot.start, slot.end, commitment.start, commitment.end):
                self._invalid(
                    f'commitment_conflict:{commitment.commitment_id}',
                    f'shift {slot.shift_id} overlaps commitment {commitment.commitment_id}',
                    'shift_commitment',
                )

    def build(self) -> Answer:
        ready = self._course_leg()
        missing = tuple(sorted(set(self.opportunity.prerequisites) - self.earned))
        if missing:
            self._invalid(
                'opportunity_prerequisite_missing:' + ','.join(missing),
                'missing opportunity prerequisites: ' + ', '.join(missing),
                'opportunity_prerequisite',
            )
        interview_end = None
        if self.opportunity.interview is not None:
            _, interview_end = self._interview_leg(ready)
        if self.opportunity.shift is not None:
            self._shift_leg(ready, interview_end)

        if self.invalid:
            individual = ReachStatus.INVALID.value
        elif self.blocked:
            individual = ReachStatus.BLOCKED.value
        elif not self.completed:
            individual = ReachStatus.THEORETICAL.value
            self.details.setdefault(
                'course_not_completed',
                'no completion evidence; schedule is only theoretically feasible',
            )
        else:
            individual = ReachStatus.FULFILLED.value

        reasons = self.invalid + [code for code in self.blocked if code not in self.invalid]
        if individual == ReachStatus.THEORETICAL.value:
            reasons = reasons + ['course_not_completed']
        return Answer(
            person_id=self.person.person_id,
            cohort_id=self.cohort_id,
            course_id=self.course_id,
            segment=self.person.segment,
            opportunity_id=self.opportunity.opportunity_id,
            kind=self.opportunity.kind,
            completed=self.completed,
            completed_at=self.completion_at,
            individual_status=individual,
            status=individual,
            counted=False,
            reasons=tuple(reasons),
            details=dict(self.details),
            path=tuple(self.path),
            failed_step=self.failed_step,
            interview_id=self.opportunity.interview.interview_id if self.opportunity.interview else None,
            shift_id=self.opportunity.shift.shift_id if self.opportunity.shift else None,
            capacity={key: dict(value) for key, value in self.capacity.items()},
        )


def _person_outcome(
    person: Person,
    course_id: str | None,
    cohort_id: str | None,
    answers: Sequence[Answer],
) -> dict[str, Any]:
    counted = [item for item in answers if item.counted]
    feasible = [item for item in answers if item.individual_status == ReachStatus.FULFILLED.value]
    # A person is summarised by the best-known route: any counted chain wins,
    # then a conditionally feasible one, then unknown facts, and only when every
    # route has a concrete rule failure is the person marked invalid.
    individual_statuses = {item.status for item in answers}
    if counted:
        status = ReachStatus.FULFILLED.value
    elif ReachStatus.THEORETICAL.value in individual_statuses:
        status = ReachStatus.THEORETICAL.value
    elif ReachStatus.BLOCKED.value in individual_statuses or not answers:
        status = ReachStatus.BLOCKED.value
    else:
        status = ReachStatus.INVALID.value

    blockers: dict[str, int] = {}
    categories: dict[str, int] = {}
    for item in answers:
        for reason in item.reasons:
            blockers[reason] = blockers.get(reason, 0) + 1
            category = blocker_category(reason)
            categories[category] = categories.get(category, 0) + 1
    if not answers:
        blockers['no_opportunity_defined'] = 1
        categories['data'] = 1

    return {
        'person_id': person.person_id,
        'cohort_id': cohort_id,
        'course_id': course_id,
        'segment': person.segment,
        'origin': person.origin,
        'completed': person.completed_at is not None,
        'completed_at': _iso(person.completed_at),
        'status': status,
        'realized_count': 1 if counted else 0,
        'realized_opportunity_id': counted[0].opportunity_id if counted else None,
        'suppressed_opportunities': [
            item.opportunity_id
            for item in feasible
            if not item.counted and not _is_capacity_lost(item)
        ],
        'opportunity_statuses': {item.opportunity_id: item.status for item in answers},
        'blockers': dict(sorted(blockers.items())),
        'blocker_categories': dict(sorted(categories.items())),
    }


def _empty_group() -> dict[str, Any]:
    return {
        'people': 0,
        'completed': 0,
        'realized': 0,
        'status_counts': {status: 0 for status in STATUS_ORDER},
        'link_status_counts': {status: 0 for status in STATUS_ORDER},
        'suppressed_links': 0,
        'capacity_lost_links': 0,
        'rates': {
            'completion_rate': None,
            'reach_rate': None,
            'fulfilled_after_completion_rate': None,
            'theoretical_rate': None,
            'invalid_rate': None,
            'blocked_rate': None,
        },
        'blockers': {},
        'blocker_categories': {},
        'failed_links_by_category': {category: 0 for category in BLOCKER_CATEGORIES},
    }


def _failure_counts(links: Sequence[Answer]) -> dict[str, int]:
    """Count links by failure category, so time loss and quota loss stay separate.

    Realized links and feasible duplicates suppressed only because the person is
    already counted are not failures and are excluded.
    """

    counted_persons = {link.person_id for link in links if link.counted}
    counts = {category: 0 for category in BLOCKER_CATEGORIES}
    for link in links:
        if link.counted or _is_suppressed(link, counted_persons):
            continue
        categories = {blocker_category(reason) for reason in link.reasons}
        if not categories:
            categories = {'data'}
        for category in categories:
            counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items()))


def _group(persons: Sequence[Mapping[str, Any]], links: Sequence[Answer]) -> dict[str, Any]:
    if not persons:
        return _empty_group()
    person_ids = {item['person_id'] for item in persons}
    group_links = [link for link in links if link.person_id in person_ids]
    counts = {status: 0 for status in STATUS_ORDER}
    for item in persons:
        counts[item['status']] = counts.get(item['status'], 0) + 1
    link_counts = {status: 0 for status in STATUS_ORDER}
    for link in group_links:
        link_counts[link.status] = link_counts.get(link.status, 0) + 1
    completed = sum(1 for item in persons if item['completed'])
    realized = sum(1 for item in persons if item['realized_count'])
    counted_persons = {item['person_id'] for item in persons if item['realized_count']}
    blockers: dict[str, int] = {}
    categories: dict[str, int] = {}
    for link in group_links:
        for reason in link.reasons:
            blockers[reason] = blockers.get(reason, 0) + 1
            category = blocker_category(reason)
            categories[category] = categories.get(category, 0) + 1
    return {
        'people': len(persons),
        'completed': completed,
        'realized': realized,
        'status_counts': counts,
        'link_status_counts': link_counts,
        'suppressed_links': sum(1 for link in group_links if _is_suppressed(link, counted_persons)),
        'capacity_lost_links': sum(1 for link in group_links if _is_capacity_lost(link)),
        'rates': {
            'completion_rate': _rate(completed, len(persons)),
            'reach_rate': _rate(realized, len(persons)),
            'fulfilled_after_completion_rate': _rate(realized, completed),
            'theoretical_rate': _rate(counts[ReachStatus.THEORETICAL.value], len(persons)),
            'invalid_rate': _rate(counts[ReachStatus.INVALID.value], len(persons)),
            'blocked_rate': _rate(counts[ReachStatus.BLOCKED.value], len(persons)),
        },
        'blockers': dict(sorted(blockers.items())),
        'blocker_categories': dict(sorted(categories.items())),
        'failed_links_by_category': _failure_counts(group_links),
    }


def _group_links(persons: Sequence[Mapping[str, Any]], links: Sequence[Answer], key: str) -> dict[str, Any]:
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for item in persons:
        bucket = str(item.get(key) or 'unassigned')
        buckets.setdefault(bucket, []).append(item)
    return {bucket: _group(rows, links) for bucket, rows in sorted(buckets.items())}


def _group_course_segment(
    persons: Sequence[Mapping[str, Any]], links: Sequence[Answer]
) -> dict[str, dict[str, Any]]:
    """Cross-tab of course and population segment, e.g. ``basic -> 女性``."""

    result: dict[str, dict[str, Any]] = {}
    for course_id in sorted({str(item.get('course_id') or 'unassigned') for item in persons}):
        rows = [item for item in persons if str(item.get('course_id') or 'unassigned') == course_id]
        result[course_id] = _group_links(rows, links, 'segment')
    return result


def _allocate(answers: Sequence[Answer], model: Model) -> tuple[list[Answer], dict[str, dict[str, Any]], dict[str, int | None]]:
    """Greedy, deterministic quota allocation used for analysis only.

    People are visited in person-id order; each person takes at most one chain.
    The function never writes a booking: it only decides which feasible chain
    this report may count as realized.
    """

    remaining: dict[str, int | None] = {}
    resources: dict[str, dict[str, Any]] = {}

    def register(key: str, slot: Any, resource: str, capacity: int | None, used: int | None, identifier: str) -> None:
        remaining[key] = None if capacity is None or used is None else max(0, capacity - used)
        resources[key] = {
            'resource': resource,
            'resource_id': identifier,
            'capacity': capacity,
            'used_at_start': used,
            'remaining_at_start': remaining[key],
        }

    for interview in model.interviews.values():
        register(
            f'interview:{interview.interview_id}',
            interview,
            'interview',
            interview.capacity,
            interview.booked,
            interview.interview_id,
        )
    for shift in model.shifts.values():
        register(
            f'shift:{shift.shift_id}',
            shift,
            'shift',
            shift.capacity,
            shift.filled,
            shift.shift_id,
        )
    # Explicit opportunities may own slots that were not declared at the top
    # level; they still consume quota in the analysis.
    for opportunity in model.opportunities:
        if opportunity.interview is not None:
            slot = opportunity.interview
            key = f'interview:{slot.interview_id}'
            if key not in resources:
                register(key, slot, 'interview', slot.capacity, slot.booked, slot.interview_id)
        if opportunity.shift is not None:
            slot = opportunity.shift
            key = f'shift:{slot.shift_id}'
            if key not in resources:
                register(key, slot, 'shift', slot.capacity, slot.filled, slot.shift_id)

    interview_starts = {key: slot.effective_start for key, slot in model.interviews.items()}
    shift_starts = {key: slot.start for key, slot in model.shifts.items()}
    for opportunity in model.opportunities:
        if opportunity.interview is not None:
            interview_starts.setdefault(opportunity.interview.interview_id, opportunity.interview.effective_start)
        if opportunity.shift is not None:
            shift_starts.setdefault(opportunity.shift.shift_id, opportunity.shift.start)

    by_person: dict[str, list[Answer]] = {}
    for answer in answers:
        if answer.individual_status == ReachStatus.FULFILLED.value:
            by_person.setdefault(answer.person_id, []).append(answer)
    for person_id in by_person:
        by_person[person_id].sort(
            key=lambda item: (
                interview_starts.get(item.interview_id) or EPOCH,
                0 if item.shift_id else 1,
                shift_starts.get(item.shift_id) or EPOCH,
                item.opportunity_id,
            )
        )

    result = list(answers)
    index = {(item.person_id, item.opportunity_id): position for position, item in enumerate(result)}
    for person_id in sorted(by_person):
        chosen = None
        for candidate in by_person[person_id]:
            keys = []
            if candidate.interview_id:
                keys.append(f'interview:{candidate.interview_id}')
            if candidate.shift_id:
                keys.append(f'shift:{candidate.shift_id}')
            if any(remaining[key] is None for key in keys):
                continue
            if all(remaining[key] > 0 for key in keys):
                chosen = candidate
                for key in keys:
                    remaining[key] -= 1
                break
        if chosen is not None:
            position = index[(chosen.person_id, chosen.opportunity_id)]
            result[position] = replace(result[position], counted=True, status=ReachStatus.FULFILLED.value)
            continue
        for candidate in by_person[person_id]:
            position = index[(candidate.person_id, candidate.opportunity_id)]
            reasons = candidate.reasons + ('capacity_allocated_elsewhere',)
            details = dict(candidate.details)
            details.setdefault(
                'capacity_allocated_elsewhere',
                'feasible chain lost the remaining slot to an earlier allocation',
            )
            result[position] = replace(
                result[position],
                status=ReachStatus.INVALID.value,
                reasons=reasons,
                details=details,
                failed_step='capacity_allocation',
            )
    for key, item in resources.items():
        item['remaining_after_allocation'] = remaining[key]
    return result, resources, remaining


def _capacity_pressure(answers: Sequence[Answer], model: Model) -> tuple[dict[str, Any], ...]:
    """Show where more people fit than there are places, per resource.

    Pressure is aggregated per interview/shift so a slot shared by several
    bundles is not reported twice.  It is a comparison of the snapshot capacity
    with the number of people whose whole chain fits, not a reservation.
    """

    resources: dict[str, dict[str, Any]] = {}

    def track(resource: str, slot: Any) -> None:
        identifier = getattr(slot, f'{resource}_id')
        key = f'{resource}:{identifier}'
        capacity = getattr(slot, 'capacity', None)
        used = getattr(slot, 'booked' if resource == 'interview' else 'filled', None)
        if capacity is None or used is None:
            return
        resources[key] = {
            'resource': resource,
            'resource_id': identifier,
            'remaining': max(0, capacity - used),
            'people': set(),
            'opportunities': set(),
        }

    for opportunity in model.opportunities:
        if opportunity.interview is not None:
            track('interview', opportunity.interview)
        if opportunity.shift is not None:
            track('shift', opportunity.shift)
    for interview in model.interviews.values():
        if f'interview:{interview.interview_id}' not in resources:
            track('interview', interview)
    for shift in model.shifts.values():
        if f'shift:{shift.shift_id}' not in resources:
            track('shift', shift)

    for answer in answers:
        if answer.individual_status != ReachStatus.FULFILLED.value:
            continue
        for key in (
            f'interview:{answer.interview_id}' if answer.interview_id else None,
            f'shift:{answer.shift_id}' if answer.shift_id else None,
        ):
            if key is None or key not in resources:
                continue
            resources[key]['people'].add(answer.person_id)
            resources[key]['opportunities'].add(answer.opportunity_id)

    pressure = []
    for key, item in sorted(resources.items()):
        feasible = sorted(item['people'])
        if len(feasible) <= item['remaining']:
            continue
        pressure.append(
            {
                'resource': item['resource'],
                'resource_id': item['resource_id'],
                'remaining': item['remaining'],
                'feasible_people': len(feasible),
                'shortfall': len(feasible) - item['remaining'],
                'people': feasible,
                'opportunities': sorted(item['opportunities']),
            }
        )
    return tuple(pressure)


def _targets(opportunity: Opportunity, person: Person) -> bool:
    """Whether an opportunity is addressed to this person's cohort."""

    if opportunity.person_id is not None:
        return opportunity.person_id == person.person_id
    if opportunity.cohort_id is None:
        return True
    cohort_id = person.cohort_id or person.course_id
    return opportunity.cohort_id in {person.cohort_id, person.course_id, cohort_id}


def analyze(data: Mapping[str, Any]) -> ReachabilityReport:
    """Evaluate course -> interview -> shift reachability without side effects."""

    before = safe_fingerprint(data)
    working = copy.deepcopy(data)
    model = build_model(working)
    answers: list[Answer] = []
    for person in model.people:
        for opportunity in model.opportunities:
            if not _targets(opportunity, person):
                continue
            answers.append(_Builder(model, person, opportunity).build())
    answers, resources, _ = _allocate(answers, model)

    eligible: list[Person] = []
    excluded: list[dict[str, Any]] = []
    for person in model.people:
        if model.course_for(person) is not None or any(
            answer.person_id == person.person_id and answer.course_id for answer in answers
        ):
            eligible.append(person)
        else:
            excluded.append(
                {
                    'person_id': person.person_id,
                    'cohort_id': person.cohort_id,
                    'segment': person.segment,
                    'reason': 'course_not_assigned',
                }
            )

    person_rows: list[dict[str, Any]] = []
    for person in eligible:
        own = [answer for answer in answers if answer.person_id == person.person_id]
        cohort = model.cohort_for(person)
        cohort_id = person.cohort_id or (cohort.cohort_id if cohort else None)
        course = model.course_for(person)
        course_id = (
            person.course_id
            or (cohort.course_id if cohort is not None else None)
            or (course.course_id if course is not None else None)
            or (own[0].course_id if own else None)
        )
        person_rows.append(_person_outcome(person, course_id, cohort_id, own))

    eligible_ids = {person.person_id for person in eligible}
    eligible_links = [answer for answer in answers if answer.person_id in eligible_ids]
    status_counts = {status: 0 for status in STATUS_ORDER}
    link_status_counts = {status: 0 for status in STATUS_ORDER}
    for row in person_rows:
        status_counts[row['status']] = status_counts.get(row['status'], 0) + 1
    for link in eligible_links:
        link_status_counts[link.status] = link_status_counts.get(link.status, 0) + 1

    completed = sum(1 for row in person_rows if row['completed'])
    realized = sum(1 for row in person_rows if row['realized_count'])
    counted_persons = {row['person_id'] for row in person_rows if row['realized_count']}
    totals = {
        'persons': len(person_rows),
        'completed_persons': completed,
        'realized_arrangements': realized,
        'theoretical_arrangements': status_counts[ReachStatus.THEORETICAL.value],
        'invalid_arrangements': status_counts[ReachStatus.INVALID.value],
        'blocked_arrangements': status_counts[ReachStatus.BLOCKED.value],
        'status_counts': status_counts,
        'links': len(eligible_links),
        'link_status_counts': link_status_counts,
        'suppressed_links': sum(1 for link in eligible_links if _is_suppressed(link, counted_persons)),
        'capacity_lost_links': sum(1 for link in eligible_links if _is_capacity_lost(link)),
        'excluded_persons': len(excluded),
        'failed_links_by_category': _failure_counts(eligible_links),
        'rates': {
            'completion_rate': _rate(completed, len(person_rows)),
            'reach_rate': _rate(realized, len(person_rows)),
            'fulfilled_after_completion_rate': _rate(realized, completed),
            'theoretical_rate': _rate(status_counts[ReachStatus.THEORETICAL.value], len(person_rows)),
            'invalid_rate': _rate(status_counts[ReachStatus.INVALID.value], len(person_rows)),
            'blocked_rate': _rate(status_counts[ReachStatus.BLOCKED.value], len(person_rows)),
        },
    }

    fingerprint_after = safe_fingerprint(data)
    notes = (
        'realized_arrangements counts each person at most once; extra feasible links are suppressed, not added.',
        'theoretical links lack completion evidence and are never counted as realized.',
        'missing travel, location, window or capacity information is reported as blocked, never assumed feasible.',
        'quota allocation is an analysis ordering only; no booking, schedule or commitment is written or changed.',
        'capacity_allocation shows the snapshot and the analysis-only remaining places, not a live reservation.',
    )
    return ReachabilityReport(
        scenario=model.scenario,
        as_of=model.as_of,
        input_fingerprint=before,
        fingerprint_after=fingerprint_after,
        inputs_unchanged=before == fingerprint_after,
        allocation_policy='person_id order; each person takes the earliest feasible interview, then earliest shift, then opportunity id',
        persons=tuple(person_rows),
        links=tuple(eligible_links),
        excluded_people=tuple(excluded),
        totals=totals,
        by_course=_group_links(person_rows, eligible_links, 'course_id'),
        by_cohort=_group_links(person_rows, eligible_links, 'cohort_id'),
        by_segment=_group_links(person_rows, eligible_links, 'segment'),
        by_course_segment=_group_course_segment(person_rows, eligible_links),
        capacity_pressure=_capacity_pressure(eligible_links, model),
        capacity_allocation=tuple(
            {'resource_key': key, **value} for key, value in sorted(resources.items())
        ),
        notes=notes,
    )


def compare_reports(left: ReachabilityReport, right: ReachabilityReport) -> dict[str, Any]:
    """Compare two course-schedule scenarios for downstream opportunities."""

    left_persons = {row['person_id']: row for row in left.persons}
    right_persons = {row['person_id']: row for row in right.persons}
    gained: list[dict[str, Any]] = []
    lost: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    for person_id in sorted(set(left_persons) | set(right_persons)):
        before = left_persons.get(person_id)
        after = right_persons.get(person_id)
        if before is None:
            gained.append({'person_id': person_id, 'to': after['status'], 'reason': 'new_in_right'})
        elif after is None:
            lost.append({'person_id': person_id, 'from': before['status'], 'reason': 'missing_in_right'})
        elif (
            before['status'] != after['status']
            or before['realized_opportunity_id'] != after['realized_opportunity_id']
        ):
            entry = {
                'person_id': person_id,
                'from': before['status'],
                'to': after['status'],
                'realized_from': before['realized_opportunity_id'],
                'realized_to': after['realized_opportunity_id'],
            }
            changed.append(entry)
            if after['status'] == ReachStatus.FULFILLED.value and before['status'] != ReachStatus.FULFILLED.value:
                gained.append(entry)
            elif before['status'] == ReachStatus.FULFILLED.value and after['status'] != ReachStatus.FULFILLED.value:
                lost.append(entry)

    course_keys = sorted(set(left.by_course) | set(right.by_course))
    by_course = {}
    for key in course_keys:
        before = left.by_course.get(key, {})
        after = right.by_course.get(key, {})
        by_course[key] = {
            'left': before,
            'right': after,
            'delta': {
                'people': int(after.get('people', 0)) - int(before.get('people', 0)),
                'completed': int(after.get('completed', 0)) - int(before.get('completed', 0)),
                'realized': int(after.get('realized', 0)) - int(before.get('realized', 0)),
            },
        }

    segment_keys = sorted(
        {
            (course, segment)
            for report in (left, right)
            for course, segments in report.by_course_segment.items()
            for segment in segments
        }
    )
    by_course_segment = {}
    for course, segment in segment_keys:
        before = left.by_course_segment.get(course, {}).get(segment, {})
        after = right.by_course_segment.get(course, {}).get(segment, {})
        by_course_segment.setdefault(course, {})[segment] = {
            'left_realized': int(before.get('realized', 0)),
            'right_realized': int(after.get('realized', 0)),
            'delta_realized': int(after.get('realized', 0)) - int(before.get('realized', 0)),
            'left_people': int(before.get('people', 0)),
            'right_people': int(after.get('people', 0)),
        }

    opportunity_keys = sorted(
        {link.opportunity_id for link in left.links} | {link.opportunity_id for link in right.links}
    )
    opportunities = {}
    for key in opportunity_keys:
        before = [link for link in left.links if link.opportunity_id == key]
        after = [link for link in right.links if link.opportunity_id == key]
        if not before or not after:
            entry = {'left': len(before), 'right': len(after)}
            entry['added' if not before else 'removed'] = True
            opportunities[key] = entry
            continue
        before_counts = {status: 0 for status in STATUS_ORDER}
        after_counts = {status: 0 for status in STATUS_ORDER}
        for link in before:
            before_counts[link.status] += 1
        for link in after:
            after_counts[link.status] += 1
        realized_left = sum(link.counted for link in before)
        realized_right = sum(link.counted for link in after)
        if before_counts != after_counts or realized_left != realized_right:
            opportunities[key] = {
                'left': before_counts,
                'right': after_counts,
                'realized_left': realized_left,
                'realized_right': realized_right,
            }

    return {
        'left': {'scenario': left.scenario, 'totals': dict(left.totals), 'by_course': dict(left.by_course)},
        'right': {'scenario': right.scenario, 'totals': dict(right.totals), 'by_course': dict(right.by_course)},
        'deltas': {
            'realized_arrangements': int(right.totals.get('realized_arrangements', 0))
            - int(left.totals.get('realized_arrangements', 0)),
            'completed_persons': int(right.totals.get('completed_persons', 0))
            - int(left.totals.get('completed_persons', 0)),
            'theoretical_arrangements': int(right.totals.get('theoretical_arrangements', 0))
            - int(left.totals.get('theoretical_arrangements', 0)),
            'invalid_arrangements': int(right.totals.get('invalid_arrangements', 0))
            - int(left.totals.get('invalid_arrangements', 0)),
            'blocked_arrangements': int(right.totals.get('blocked_arrangements', 0))
            - int(left.totals.get('blocked_arrangements', 0)),
            'suppressed_links': int(right.totals.get('suppressed_links', 0))
            - int(left.totals.get('suppressed_links', 0)),
            'capacity_lost_links': int(right.totals.get('capacity_lost_links', 0))
            - int(left.totals.get('capacity_lost_links', 0)),
        },
        'by_course': by_course,
        'by_course_segment': by_course_segment,
        'persons': {'gained': gained, 'lost': lost, 'changed': changed},
        'opportunities': opportunities,
    }


class ReachabilityAnalyzer:
    """Public facade used by the CLI and by advisers."""

    def analyze(self, data: Mapping[str, Any]) -> ReachabilityReport:
        if not isinstance(data, Mapping):
            raise ReachabilityError('analysis input must be a mapping')
        return analyze(data)

    def compare(self, left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
        return compare_reports(self.analyze(left), self.analyze(right))

    def cohort_detail(self, report: ReachabilityReport, cohort_id: str) -> dict[str, Any]:
        group = report.by_cohort.get(cohort_id)
        if group is None:
            raise ReachabilityError(f'unknown cohort: {cohort_id}')
        counted = report._counted_persons()
        links = [link.as_dict() for link in report.links if link.cohort_id == cohort_id]
        return {
            'cohort_id': cohort_id,
            'summary': group,
            'links': links,
            'blocked_paths': [
                link.as_dict()
                for link in report.links
                if link.cohort_id == cohort_id and not link.counted
            ],
            'suppressed_links': [
                link.as_dict() for link in report.links if link.cohort_id == cohort_id and _is_suppressed(link, counted)
            ],
        }

    def person_detail(self, report: ReachabilityReport, person_id: str) -> dict[str, Any]:
        rows = [row for row in report.persons if row['person_id'] == person_id]
        if not rows:
            raise ReachabilityError(f'unknown person: {person_id}')
        counted = report._counted_persons()
        links = [link.as_dict() for link in report.links if link.person_id == person_id]
        return {
            'person': rows[0],
            'links': links,
            'blocked_paths': [
                link.as_dict() for link in report.links if link.person_id == person_id and not link.counted
            ],
            'suppressed_links': [
                link.as_dict() for link in report.links if link.person_id == person_id and _is_suppressed(link, counted)
            ],
        }


def analyze_opportunities(data: Mapping[str, Any]) -> dict[str, Any]:
    """Convenience wrapper returning the JSON-ready report payload."""

    return ReachabilityAnalyzer().analyze(data).as_dict()


OpportunityReachabilityAnalyzer = ReachabilityAnalyzer
