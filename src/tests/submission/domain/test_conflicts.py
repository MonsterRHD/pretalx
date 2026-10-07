# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
import datetime as dt

import pytest
from django_scopes import scope, scopes_disabled

from pretalx.schedule.domain.release import freeze_schedule
from pretalx.submission.domain.conflicts import (
    find_signup_conflicts,
    get_conflicting_signup,
    intervals_overlap,
)
from pretalx.submission.enums import AttendeeSignupStates, SubmissionStates
from tests.factories import (
    AttendeeProfileFactory,
    AttendeeSignupFactory,
    EventFactory,
    RoomFactory,
    SubmissionFactory,
    SubmissionTypeFactory,
    TalkSlotFactory,
    UserFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def _signup_event():
    event = EventFactory(feature_flags={"attendee_signup": True})
    sub_type = SubmissionTypeFactory(event=event, attendee_signup_required=True)
    return event, sub_type


def _session(
    event, sub_type, *, start, end, room=None, state=SubmissionStates.CONFIRMED
):
    submission = SubmissionFactory(
        event=event, submission_type=sub_type, attendee_signup_capacity=10, state=state
    )
    TalkSlotFactory(
        submission=submission,
        room=room or RoomFactory(event=event, capacity=20),
        schedule=event.wip_schedule,
        is_visible=True,
        start=start,
        end=end,
    )
    return submission


def _pair(event, sub_type, *, window_a, window_b, release=True):
    base = event.datetime_from
    submission_a = _session(
        event,
        sub_type,
        start=base + dt.timedelta(minutes=window_a[0]),
        end=base + dt.timedelta(minutes=window_a[1]),
    )
    submission_b = _session(
        event,
        sub_type,
        start=base + dt.timedelta(minutes=window_b[0]),
        end=base + dt.timedelta(minutes=window_b[1]),
    )
    if release:
        with scopes_disabled():
            freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)
    return submission_a, submission_b


def _attendee_with_signups(event, submissions, *, state=AttendeeSignupStates.CONFIRMED):
    user = UserFactory()
    profile = AttendeeProfileFactory(event=event, user=user)
    signups = [
        AttendeeSignupFactory(submission=submission, attendee=profile, state=state)
        for submission in submissions
    ]
    return profile, signups


@pytest.mark.parametrize(
    ("sa", "ea", "sb", "eb", "expected"),
    (
        (0, 60, 30, 90, True),
        (0, 60, 0, 60, True),
        (0, 60, 60, 120, False),
        (30, 90, 0, 30, False),
        (0, 30, 60, 90, False),
        (0, None, 30, 90, False),
        (None, 60, 30, 90, False),
    ),
)
def test_intervals_overlap_half_open(sa, ea, sb, eb, expected):
    assert intervals_overlap(sa, ea, sb, eb) is expected


def test_find_conflicts_detects_same_attendee_overlap_across_rooms():
    event, sub_type = _signup_event()
    submission_a, submission_b = _pair(
        event, sub_type, window_a=(0, 60), window_b=(30, 90)
    )
    profile, (signup_a, signup_b) = _attendee_with_signups(
        event, [submission_a, submission_b]
    )

    with scope(event=event):
        conflicts = find_signup_conflicts(event.current_schedule)

    assert len(conflicts) == 1
    entry = conflicts[0]
    assert entry["attendee"] == profile
    assert {entry["submission_a"], entry["submission_b"]} == {
        submission_a,
        submission_b,
    }
    assert {entry["signup_a"], entry["signup_b"]} == {signup_a, signup_b}
    assert entry["slot_a"].submission_id in {submission_a.pk, submission_b.pk}


def test_find_conflicts_back_to_back_sessions_are_allowed():
    event, sub_type = _signup_event()
    submission_a, submission_b = _pair(
        event, sub_type, window_a=(0, 60), window_b=(60, 120)
    )
    _attendee_with_signups(event, [submission_a, submission_b])

    with scope(event=event):
        assert find_signup_conflicts(event.current_schedule) == []


def test_find_conflicts_different_attendees_never_conflict():
    event, sub_type = _signup_event()
    submission_a, submission_b = _pair(
        event, sub_type, window_a=(0, 60), window_b=(30, 90)
    )
    _attendee_with_signups(event, [submission_a])
    _attendee_with_signups(event, [submission_b])

    with scope(event=event):
        assert find_signup_conflicts(event.current_schedule) == []


def test_find_conflicts_ignores_cancelled_signups():
    event, sub_type = _signup_event()
    submission_a, submission_b = _pair(
        event, sub_type, window_a=(0, 60), window_b=(30, 90)
    )
    _attendee_with_signups(
        event, [submission_a, submission_b], state=AttendeeSignupStates.CANCELED
    )

    with scope(event=event):
        assert find_signup_conflicts(event.current_schedule) == []


def test_find_conflicts_one_cancelled_signup_is_not_conflict():
    event, sub_type = _signup_event()
    submission_a, submission_b = _pair(
        event, sub_type, window_a=(0, 60), window_b=(30, 90)
    )
    user = UserFactory()
    profile = AttendeeProfileFactory(event=event, user=user)
    AttendeeSignupFactory(submission=submission_a, attendee=profile)
    AttendeeSignupFactory(
        submission=submission_b, attendee=profile, state=AttendeeSignupStates.CANCELED
    )

    with scope(event=event):
        assert find_signup_conflicts(event.current_schedule) == []


def test_find_conflicts_ignores_timeless_slots():
    event, sub_type = _signup_event()
    base = event.datetime_from
    timed = _session(event, sub_type, start=base, end=base + dt.timedelta(hours=1))
    timeless = _session(event, sub_type, start=None, end=None)
    with scopes_disabled():
        freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)
    _attendee_with_signups(event, [timed, timeless])

    with scope(event=event):
        assert find_signup_conflicts(event.current_schedule) == []


def test_find_conflicts_released_schedule_ignores_invisible_slot():
    event, sub_type = _signup_event()
    submission_a, submission_b = _pair(
        event, sub_type, window_a=(0, 60), window_b=(30, 90)
    )
    _attendee_with_signups(event, [submission_a, submission_b])
    with scopes_disabled():
        event.current_schedule.talks.filter(submission=submission_b).update(
            is_visible=False
        )

    with scope(event=event):
        assert find_signup_conflicts(event.current_schedule) == []


def test_find_conflicts_wip_ignores_unconfirmed_submission():
    event, sub_type = _signup_event()
    base = event.datetime_from
    confirmed = _session(event, sub_type, start=base, end=base + dt.timedelta(hours=1))
    unconfirmed = _session(
        event,
        sub_type,
        start=base + dt.timedelta(minutes=30),
        end=base + dt.timedelta(hours=2),
        state=SubmissionStates.SUBMITTED,
    )
    # WIP visibility mirrors what freeze would publish: only confirmed.
    _attendee_with_signups(event, [confirmed, unconfirmed])

    with scope(event=event):
        assert find_signup_conflicts(event.wip_schedule) == []


def test_get_conflicting_signup_returns_overlapping_signup():
    event, sub_type = _signup_event()
    submission_a, submission_b = _pair(
        event, sub_type, window_a=(0, 60), window_b=(30, 90)
    )
    profile, (signup_a, _signup_b) = _attendee_with_signups(
        event, [submission_a, submission_b]
    )

    with scope(event=event):
        result = get_conflicting_signup(profile, event.current_schedule, submission_b)

    assert result == signup_a


def test_get_conflicting_signup_none_without_overlap():
    event, sub_type = _signup_event()
    submission_a, submission_b = _pair(
        event, sub_type, window_a=(0, 60), window_b=(60, 120)
    )
    profile, _signups = _attendee_with_signups(event, [submission_a, submission_b])

    with scope(event=event):
        assert (
            get_conflicting_signup(profile, event.current_schedule, submission_b)
            is None
        )


def test_get_conflicting_signup_ignores_signup_for_target_itself():
    event, sub_type = _signup_event()
    submission, _other = _pair(event, sub_type, window_a=(0, 60), window_b=(30, 90))
    profile = AttendeeProfileFactory(event=event, user=UserFactory())
    AttendeeSignupFactory(submission=submission, attendee=profile)

    with scope(event=event):
        # The attendee's own signup on the target session is not a
        # conflict with itself.
        assert (
            get_conflicting_signup(profile, event.current_schedule, submission) is None
        )


def test_find_conflicts_ignores_break_slots():
    event, sub_type = _signup_event()
    base = event.datetime_from
    # A signup session and a break at the exact same time.
    session = _session(
        event, sub_type, start=base, end=base + dt.timedelta(hours=1)
    )
    TalkSlotFactory(
        submission=None,
        schedule=event.wip_schedule,
        room=RoomFactory(event=event),
        start=base,
        end=base + dt.timedelta(hours=1),
        is_visible=True,
    )
    with scopes_disabled():
        freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)
    profile = AttendeeProfileFactory(event=event, user=UserFactory())
    AttendeeSignupFactory(submission=session, attendee=profile)

    with scope(event=event):
        assert find_signup_conflicts(event.current_schedule) == []


def test_get_conflicting_signup_none_when_target_not_visible():
    event, sub_type = _signup_event()
    submission_a, submission_b = _pair(
        event, sub_type, window_a=(0, 60), window_b=(30, 90)
    )
    profile, _signups = _attendee_with_signups(event, [submission_a, submission_b])
    with scopes_disabled():
        event.current_schedule.talks.filter(submission=submission_b).update(
            is_visible=False
        )

    with scope(event=event):
        assert (
            get_conflicting_signup(profile, event.current_schedule, submission_b)
            is None
        )
