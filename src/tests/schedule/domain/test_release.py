# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
import datetime as dt

import pytest
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django_scopes import scope

from pretalx.common.exceptions import ScheduleConflictError
from pretalx.mail.models import QueuedMail
from pretalx.schedule.domain.release import (
    apply_signup_capacity_changes,
    apply_signup_capacity_defaults,
    freeze_schedule,
    guess_schedule_version,
    unfreeze_schedule,
)
from pretalx.schedule.models.slot import SlotType
from pretalx.schedule.signals import schedule_release
from pretalx.submission.domain.signup import cancel_signup, create_signup
from pretalx.submission.enums import AttendeeSignupStates
from pretalx.submission.models import SubmissionStates
from tests.factories import (
    AttendeeProfileFactory,
    AttendeeSignupFactory,
    EventFactory,
    RoomFactory,
    ScheduleFactory,
    SpeakerFactory,
    SubmissionFactory,
    SubmissionTypeFactory,
    TalkSlotFactory,
    UserFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


@pytest.mark.parametrize(
    ("previous_version", "expected"),
    (
        (None, "0.1"),
        ("0.1", "0.2"),
        ("0,2", "0,3"),
        ("0-3", "0-4"),
        ("0_4", "0_5"),
        ("1.0.1", "1.0.2"),
        ("something.1", "something.2"),
        ("Nichtnumerisch", ""),
        ("1.something", ""),
    ),
)
def test_guess_schedule_version(previous_version, expected):
    event = EventFactory()
    if previous_version:
        ScheduleFactory(event=event, version=previous_version)
    assert guess_schedule_version(event) == expected


def test_freeze_schedule_basic():
    submission = SubmissionFactory(state=SubmissionStates.CONFIRMED)
    event = submission.event
    room = RoomFactory(event=event)
    wip = event.wip_schedule
    TalkSlotFactory(
        schedule=wip,
        submission=submission,
        room=room,
        start=event.datetime_from,
        end=event.datetime_from + dt.timedelta(hours=1),
    )

    with scope(event=event):
        released, new_wip = freeze_schedule(
            wip, "v1", comment="Release notes", notify_speakers=False
        )

    assert released.version == "v1"
    assert released.comment == "Release notes"
    assert released.published is not None
    assert new_wip.version is None
    assert released.talks.count() == 1
    assert new_wip.talks.count() == 1


@pytest.mark.parametrize(
    ("name", "match"),
    (
        ("wip", "reserved name"),
        ("latest", "reserved name"),
        ("", "without a version name"),
    ),
)
def test_freeze_schedule_rejects_invalid_name(event, name, match):
    with scope(event=event), pytest.raises(ValueError, match=match):
        freeze_schedule(event.wip_schedule, name)


def test_freeze_schedule_rejects_version_with_slash(event):
    with scope(event=event), pytest.raises(ValidationError):
        freeze_schedule(event.wip_schedule, "200 km/h")

    with scope(event=event):
        assert event.wip_schedule.version is None


def test_freeze_schedule_rejects_already_frozen():
    event = EventFactory()
    wip = event.wip_schedule
    released, _ = freeze_schedule(wip, "v1", notify_speakers=False)

    with pytest.raises(ValueError, match="already versioned"):
        freeze_schedule(released, "v2")


def test_freeze_schedule_rejects_duplicate_version(event):
    with scope(event=event):
        freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)
        with pytest.raises(IntegrityError):
            freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)


def test_freeze_schedule_sets_visibility_for_confirmed_and_breaks():
    confirmed = SubmissionFactory(state=SubmissionStates.CONFIRMED)
    event = confirmed.event
    unconfirmed = SubmissionFactory(event=event, state=SubmissionStates.ACCEPTED)
    room = RoomFactory(event=event)
    wip = event.wip_schedule
    start = event.datetime_from
    end = start + dt.timedelta(hours=1)

    TalkSlotFactory(schedule=wip, submission=confirmed, room=room, start=start, end=end)
    TalkSlotFactory(
        schedule=wip,
        submission=unconfirmed,
        room=room,
        start=start + dt.timedelta(hours=2),
        end=end + dt.timedelta(hours=2),
    )
    TalkSlotFactory(
        submission=None,
        schedule=wip,
        room=room,
        start=start + dt.timedelta(hours=4),
        end=end + dt.timedelta(hours=4),
        is_visible=False,
        slot_type=SlotType.BREAK,
        description="Coffee Break",
    )

    with scope(event=event):
        released, _ = freeze_schedule(wip, "v1", notify_speakers=False)

    confirmed_slot = released.talks.get(submission=confirmed)
    unconfirmed_slot = released.talks.get(submission=unconfirmed)
    break_slot = released.talks.get(slot_type=SlotType.BREAK)

    assert confirmed_slot.is_visible is True
    assert unconfirmed_slot.is_visible is False
    assert break_slot.is_visible is True


def test_freeze_schedule_removes_blockers_from_released():
    event = EventFactory()
    room = RoomFactory(event=event)
    wip = event.wip_schedule
    start = event.datetime_from

    TalkSlotFactory(
        submission=None,
        schedule=wip,
        room=room,
        start=start,
        end=start + dt.timedelta(hours=1),
        slot_type=SlotType.BLOCKER,
        description="Blocked",
    )

    with scope(event=event):
        released, new_wip = freeze_schedule(wip, "v1", notify_speakers=False)

    assert released.talks.filter(slot_type=SlotType.BLOCKER).count() == 0
    assert new_wip.talks.filter(slot_type=SlotType.BLOCKER).count() == 1


def test_freeze_schedule_fires_signal(event, register_signal_handler):
    received = []

    def handler(signal, sender, **kwargs):
        received.append(kwargs)

    register_signal_handler(schedule_release, handler)

    with scope(event=event):
        released, _ = freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)

    assert len(received) == 1
    assert received[0]["schedule"] == released


def test_freeze_schedule_invalidates_wip_cache():
    event = EventFactory()
    old_wip = event.wip_schedule
    freeze_schedule(old_wip, "v1", notify_speakers=False)

    assert event.wip_schedule.version is None
    assert event.wip_schedule.pk != old_wip.pk


def test_freeze_schedule_clears_unreleased_changes_flag():
    event = EventFactory()
    event.cache.set("has_unreleased_schedule_changes", True)
    freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)

    assert event.cache.get("has_unreleased_schedule_changes") is False


def test_freeze_schedule_with_notify_speakers():
    submission = SubmissionFactory(state=SubmissionStates.CONFIRMED)
    event = submission.event
    speaker = SpeakerFactory(event=event)
    submission.speakers.add(speaker)
    room = RoomFactory(event=event)
    wip = event.wip_schedule
    TalkSlotFactory(
        schedule=wip,
        submission=submission,
        room=room,
        start=event.datetime_from,
        end=event.datetime_from + dt.timedelta(hours=1),
    )

    with scope(event=event):
        released, _ = freeze_schedule(wip, "v1", notify_speakers=True)

    assert QueuedMail.objects.filter(to_speakers=speaker).count() == 1


def test_unfreeze_schedule_basic():
    submission = SubmissionFactory(state=SubmissionStates.CONFIRMED)
    event = submission.event
    room = RoomFactory(event=event)
    start = event.datetime_from
    end = start + dt.timedelta(hours=1)

    TalkSlotFactory(
        schedule=event.wip_schedule,
        submission=submission,
        room=room,
        start=start,
        end=end,
    )
    released_v1, _ = freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)

    # Modify WIP: remove the slot
    wip = event.wip_schedule
    wip.talks.update(room=None, start=None, end=None)
    _, _ = freeze_schedule(wip, "v2", notify_speakers=False)

    _, new_wip = unfreeze_schedule(released_v1)

    assert new_wip.version is None
    assert new_wip.talks.filter(submission=submission).count() == 1


def test_unfreeze_schedule_rejects_wip(event):
    with scope(event=event), pytest.raises(ValueError, match="not released yet"):
        unfreeze_schedule(event.wip_schedule)


def test_unfreeze_schedule_preserves_talks_from_both_versions():
    sub1 = SubmissionFactory(state=SubmissionStates.CONFIRMED)
    event = sub1.event
    room = RoomFactory(event=event)
    start = event.datetime_from
    end = start + dt.timedelta(hours=1)

    TalkSlotFactory(
        schedule=event.wip_schedule, submission=sub1, room=room, start=start, end=end
    )
    released_v1, _ = freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)

    sub2 = SubmissionFactory(event=event, state=SubmissionStates.CONFIRMED)
    wip = event.wip_schedule
    TalkSlotFactory(
        schedule=wip,
        submission=sub2,
        room=room,
        start=start + dt.timedelta(hours=2),
        end=end + dt.timedelta(hours=2),
    )
    freeze_schedule(wip, "v2", notify_speakers=False)

    _, new_wip = unfreeze_schedule(released_v1)

    assert new_wip.talks.count() == 2
    submission_ids = set(new_wip.talks.values_list("submission_id", flat=True))
    assert submission_ids == {sub1.pk, sub2.pk}


def test_unfreeze_schedule_clears_unreleased_changes_flag():
    event = EventFactory()
    released, _ = freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)
    event.cache.set("has_unreleased_schedule_changes", True)

    unfreeze_schedule(released)

    assert event.cache.get("has_unreleased_schedule_changes") is False


@pytest.fixture
def signup_event():
    event = EventFactory()
    event.feature_flags["attendee_signup"] = True
    event.save()
    return event


def _make_signup_slot(
    event,
    *,
    room_capacity,
    session_capacity=None,
    signup_required=True,
    is_visible=True,
):
    sub_type = event.cfp.default_type
    sub_type.attendee_signup_required = signup_required
    sub_type.save()
    room = RoomFactory(event=event, capacity=room_capacity)
    submission = SubmissionFactory(
        event=event,
        state=SubmissionStates.CONFIRMED,
        submission_type=sub_type,
        attendee_signup_capacity=session_capacity,
    )
    start = event.datetime_from
    slot = TalkSlotFactory(
        schedule=event.wip_schedule,
        submission=submission,
        room=room,
        start=start,
        end=start + dt.timedelta(hours=1),
        is_visible=is_visible,
    )
    return submission, slot


@pytest.mark.parametrize(
    ("feature_on", "room_capacity", "session_capacity", "signup_required", "expected"),
    (
        (False, 80, None, True, None),  # feature off: no-op
        (True, 120, None, True, 120),  # default from room capacity
        (True, 120, 80, True, 80),  # keep explicit capacity
        (True, 120, None, False, None),  # signup not required for type
        (True, None, None, True, None),  # room capacity unset
    ),
)
def test_apply_signup_capacity_defaults(
    feature_on, room_capacity, session_capacity, signup_required, expected
):
    event = EventFactory(feature_flags={"attendee_signup": feature_on})
    submission, _ = _make_signup_slot(
        event,
        room_capacity=room_capacity,
        session_capacity=session_capacity,
        signup_required=signup_required,
    )

    with scope(event=event):
        apply_signup_capacity_defaults(event.wip_schedule)

    submission.refresh_from_db()
    assert submission.attendee_signup_capacity == expected


def test_apply_signup_capacity_defaults_picks_smallest_room(signup_event):
    sub_type = signup_event.cfp.default_type
    sub_type.attendee_signup_required = True
    sub_type.save()
    big = RoomFactory(event=signup_event, capacity=200)
    small = RoomFactory(event=signup_event, capacity=50)
    submission = SubmissionFactory(
        event=signup_event, state=SubmissionStates.CONFIRMED, submission_type=sub_type
    )
    start = signup_event.datetime_from
    TalkSlotFactory(
        schedule=signup_event.wip_schedule,
        submission=submission,
        room=big,
        start=start,
        end=start + dt.timedelta(hours=1),
        is_visible=True,
    )
    TalkSlotFactory(
        schedule=signup_event.wip_schedule,
        submission=submission,
        room=small,
        start=start + dt.timedelta(hours=2),
        end=start + dt.timedelta(hours=3),
        is_visible=True,
    )

    with scope(event=signup_event):
        apply_signup_capacity_defaults(signup_event.wip_schedule)

    submission.refresh_from_db()
    assert submission.attendee_signup_capacity == 50


def test_freeze_schedule_locks_in_room_capacity(signup_event):
    submission, _ = _make_signup_slot(signup_event, room_capacity=75)

    with scope(event=signup_event):
        freeze_schedule(signup_event.wip_schedule, "v1", notify_speakers=False)

    submission.refresh_from_db()
    assert submission.attendee_signup_capacity == 75


def test_apply_signup_capacity_changes_skips_unchanged(signup_event):
    submission, _ = _make_signup_slot(
        signup_event, room_capacity=120, session_capacity=80
    )
    log_count_before = signup_event.log_entries.count()

    with scope(event=signup_event):
        apply_signup_capacity_changes(signup_event, [(submission, 80)])

    submission.refresh_from_db()
    assert submission.attendee_signup_capacity == 80
    assert signup_event.log_entries.count() == log_count_before


def test_apply_signup_capacity_defaults_includes_invisible_scheduled_slots(
    signup_event,
):
    submission, _ = _make_signup_slot(signup_event, room_capacity=120, is_visible=False)

    with scope(event=signup_event):
        apply_signup_capacity_defaults(signup_event.wip_schedule)

    submission.refresh_from_db()
    assert submission.attendee_signup_capacity == 120


def _two_signup_sessions(event, *, start_b_offset=60, end_b_offset=120):
    sub_type = SubmissionTypeFactory(event=event, attendee_signup_required=True)
    base = event.datetime_from
    room_a = RoomFactory(event=event, capacity=20)
    room_b = RoomFactory(event=event, capacity=20)
    submission_a = SubmissionFactory(
        event=event,
        submission_type=sub_type,
        state=SubmissionStates.CONFIRMED,
        attendee_signup_capacity=10,
    )
    submission_b = SubmissionFactory(
        event=event,
        submission_type=sub_type,
        state=SubmissionStates.CONFIRMED,
        attendee_signup_capacity=10,
    )
    TalkSlotFactory(
        submission=submission_a,
        room=room_a,
        schedule=event.wip_schedule,
        is_visible=True,
        start=base,
        end=base + dt.timedelta(hours=1),
    )
    TalkSlotFactory(
        submission=submission_b,
        room=room_b,
        schedule=event.wip_schedule,
        is_visible=True,
        start=base + dt.timedelta(minutes=start_b_offset),
        end=base + dt.timedelta(minutes=end_b_offset),
    )
    return submission_a, submission_b


def test_freeze_schedule_blocks_when_wip_move_creates_signup_overlap():
    event = EventFactory(feature_flags={"attendee_signup": True})
    with scope(event=event):
        submission_a, submission_b = _two_signup_sessions(event)
        freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)
        schedules_before = event.schedules.count()
        user = UserFactory()
        create_signup(submission_a, user=user)
        create_signup(submission_b, user=user)

        # Organiser moves B so that it overlaps A in the new WIP.
        wip = event.wip_schedule
        moved_slot = wip.talks.get(submission=submission_b)
        moved_slot.start = event.datetime_from + dt.timedelta(minutes=30)
        moved_slot.end = event.datetime_from + dt.timedelta(minutes=90)
        moved_slot.save(update_fields=["start", "end"])

        with pytest.raises(ScheduleConflictError) as exc_info:
            freeze_schedule(wip, "v2", notify_speakers=False)

    conflicts = exc_info.value.conflicts
    assert len(conflicts) == 1
    assert {conflicts[0]["submission_a"], conflicts[0]["submission_b"]} == {
        submission_a,
        submission_b,
    }
    with scope(event=event):
        # Nothing was published: no new version, no extra WIP schedule,
        # and the previous public schedule is still the current one.
        assert event.schedules.count() == schedules_before
        assert not event.schedules.filter(version="v2").exists()
        assert event.current_schedule.version == "v1"
        # The WIP adjustment is preserved (nothing was written by the
        # failed freeze), so the organiser can keep editing.
        moved_slot.refresh_from_db()
        assert moved_slot.start == event.datetime_from + dt.timedelta(minutes=30)


def test_freeze_schedule_blocked_release_can_be_retried_after_cancelling():
    event = EventFactory(feature_flags={"attendee_signup": True})
    with scope(event=event):
        submission_a, submission_b = _two_signup_sessions(event)
        freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)
        user = UserFactory()
        create_signup(submission_a, user=user)
        create_signup(submission_b, user=user)
        wip = event.wip_schedule
        moved_slot = wip.talks.get(submission=submission_b)
        moved_slot.start = event.datetime_from + dt.timedelta(minutes=30)
        moved_slot.end = event.datetime_from + dt.timedelta(minutes=90)
        moved_slot.save(update_fields=["start", "end"])

        with pytest.raises(ScheduleConflictError):
            freeze_schedule(wip, "v2", notify_speakers=False)

        # Resolve the conflict, then the same release attempt succeeds.
        cancel_signup(submission_b, user=user)
        released, new_wip = freeze_schedule(
            event.wip_schedule, "v2", notify_speakers=False
        )

    assert released.version == "v2"
    with scope(event=event):
        assert event.current_schedule.version == "v2"
        assert new_wip.pk != wip.pk


def test_freeze_schedule_allows_wip_move_without_signup_overlap():
    event = EventFactory(feature_flags={"attendee_signup": True})
    with scope(event=event):
        submission_a, submission_b = _two_signup_sessions(event)
        freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)
        user = UserFactory()
        create_signup(submission_a, user=user)
        create_signup(submission_b, user=user)
        wip = event.wip_schedule
        moved_slot = wip.talks.get(submission=submission_b)
        moved_slot.start = event.datetime_from + dt.timedelta(hours=2)
        moved_slot.end = event.datetime_from + dt.timedelta(hours=3)
        moved_slot.save(update_fields=["start", "end"])

        released, _wip = freeze_schedule(wip, "v2", notify_speakers=False)

    assert released.version == "v2"


def test_freeze_schedule_retry_succeeds_after_sessions_moved_apart():
    event = EventFactory(feature_flags={"attendee_signup": True})
    with scope(event=event):
        submission_a, submission_b = _two_signup_sessions(event)
        freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)
        user = UserFactory()
        create_signup(submission_a, user=user)
        create_signup(submission_b, user=user)
        wip = event.wip_schedule
        moved_slot = wip.talks.get(submission=submission_b)
        moved_slot.start = event.datetime_from + dt.timedelta(minutes=30)
        moved_slot.end = event.datetime_from + dt.timedelta(minutes=90)
        moved_slot.save(update_fields=["start", "end"])

        with pytest.raises(ScheduleConflictError):
            freeze_schedule(wip, "v2", notify_speakers=False)

        # Adjust the WIP schedule so the sessions no longer overlap; no
        # signup changes are necessary and the release now goes through.
        moved_slot.start = event.datetime_from + dt.timedelta(hours=3)
        moved_slot.end = event.datetime_from + dt.timedelta(hours=4)
        moved_slot.save(update_fields=["start", "end"])

        released, _new_wip = freeze_schedule(
            event.wip_schedule, "v2", notify_speakers=False
        )

    assert released.version == "v2"
    with scope(event=event):
        assert event.current_schedule.version == "v2"
        # Both signups survived the release unchanged.
        assert submission_a.attendee_signups.filter(
            state=AttendeeSignupStates.CONFIRMED
        ).count() == 1
        assert submission_b.attendee_signups.filter(
            state=AttendeeSignupStates.CONFIRMED
        ).count() == 1


def test_freeze_schedule_blocks_first_release_with_conflicting_signups():
    event = EventFactory(feature_flags={"attendee_signup": True})
    with scope(event=event):
        submission_a, submission_b = _two_signup_sessions(
            event, start_b_offset=30, end_b_offset=90
        )
        user = UserFactory()
        profile = AttendeeProfileFactory(event=event, user=user)
        AttendeeSignupFactory(submission=submission_a, attendee=profile)
        AttendeeSignupFactory(submission=submission_b, attendee=profile)
        assert event.current_schedule is None

        with pytest.raises(ScheduleConflictError):
            freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)

        assert event.current_schedule is None
        assert event.schedules.filter(version__isnull=False).count() == 0


def test_freeze_schedule_ignores_signup_conflicts_when_feature_disabled():
    event = EventFactory(feature_flags={"attendee_signup": False})
    with scope(event=event):
        submission_a, submission_b = _two_signup_sessions(
            event, start_b_offset=30, end_b_offset=90
        )
        user = UserFactory()
        profile = AttendeeProfileFactory(event=event, user=user)
        AttendeeSignupFactory(submission=submission_a, attendee=profile)
        AttendeeSignupFactory(submission=submission_b, attendee=profile)

        released, _wip = freeze_schedule(
            event.wip_schedule, "v1", notify_speakers=False
        )

    assert released.version == "v1"


def test_freeze_schedule_conflict_only_counts_confirmed_signups():
    event = EventFactory(feature_flags={"attendee_signup": True})
    with scope(event=event):
        submission_a, submission_b = _two_signup_sessions(
            event, start_b_offset=30, end_b_offset=90
        )
        user = UserFactory()
        profile = AttendeeProfileFactory(event=event, user=user)
        AttendeeSignupFactory(submission=submission_a, attendee=profile)
        AttendeeSignupFactory(
            submission=submission_b,
            attendee=profile,
            state=AttendeeSignupStates.CANCELED,
        )

        released, _wip = freeze_schedule(
            event.wip_schedule, "v1", notify_speakers=False
        )

    assert released.version == "v1"
