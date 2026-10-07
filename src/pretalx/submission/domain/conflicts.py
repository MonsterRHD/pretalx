# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

"""Single source of truth for overlapping attendee signup conflicts.

The invariant enforced everywhere in pretalx: one attendee can hold at
most one *confirmed* signup for sessions whose publicly visible slots
overlap in time, within one public schedule generation.

* Signup creation checks the currently released schedule.
* Schedule release checks the schedule that is about to become public.
* Release warnings, the organiser release page, the API, public pages,
  widgets and exports all rely on the same predicate below, so they can
  never disagree about what counts as a conflict.

Only timed, visible slots of actual sessions participate: timeless
slots, breaks/blockers and sessions that do not require signup are
ignored (the latter never have signups in the first place).
"""

from collections import defaultdict

from pretalx.submission.enums import AttendeeSignupStates, SubmissionStates
from pretalx.submission.models import AttendeeSignup


def intervals_overlap(start_a, end_a, start_b, end_b):
    """Half-open interval overlap, mirroring room/speaker overlap checks.

    Back-to-back intervals (one ends when the other starts) do not
    overlap. ``None`` bounds never overlap.
    """
    if start_a is None or start_b is None or end_a is None or end_b is None:
        return False
    return start_a < end_b and start_b < end_a


def slots_overlap(slot_a, slot_b):
    """Overlap based on actual start and effective end.

    ``real_end`` falls back to the session duration when the slot has no
    explicit end stored.
    """
    return intervals_overlap(
        slot_a.start, slot_a.real_end, slot_b.start, slot_b.real_end
    )


def participating_slots(schedule):
    """Timed session slots that count for signup conflicts in ``schedule``.

    Released schedules trust their frozen ``is_visible`` flags. For a WIP
    schedule we mirror what :func:`pretalx.schedule.domain.release.freeze_schedule`
    is about to publish (confirmed submissions with a start), because WIP
    visibility flags are stale for newly scheduled or moved sessions.
    """
    queryset = schedule.talks.filter(
        submission__isnull=False,
        start__isnull=False,
    )
    if schedule.version:
        queryset = queryset.filter(is_visible=True)
    else:
        queryset = queryset.filter(
            submission__state=SubmissionStates.CONFIRMED,
        )
    return queryset.select_related(
        "room",
        "submission",
        "submission__event",
        "submission__submission_type",
        "submission__track",
    ).order_by("start", "id")


def _slots_by_submission(slots):
    result = defaultdict(list)
    for slot in slots:
        result[slot.submission_id].append(slot)
    return result


def get_conflicting_signup(attendee, schedule, submission):
    """Return the attendee's confirmed signup overlapping ``submission``.

    Checks every visible, timed slot of ``submission`` in ``schedule``
    against the visible slots of every other session the attendee has a
    confirmed signup for. Returns the conflicting ``AttendeeSignup`` (or
    ``None``), and never the signup for ``submission`` itself.
    """
    other_signups = list(
        AttendeeSignup.objects.filter(
            attendee=attendee,
            state=AttendeeSignupStates.CONFIRMED,
        )
        .exclude(submission=submission)
        .select_related("submission")
    )
    if not other_signups:
        return None
    submission_ids = [submission.pk] + [
        signup.submission_id for signup in other_signups
    ]
    slots = list(participating_slots(schedule).filter(submission_id__in=submission_ids))
    target_slots = [slot for slot in slots if slot.submission_id == submission.pk]
    if not target_slots:
        return None
    other_slots = _slots_by_submission(
        slot for slot in slots if slot.submission_id != submission.pk
    )
    for signup in sorted(other_signups, key=lambda item: item.submission_id):
        for other_slot in other_slots.get(signup.submission_id, ()):
            if any(slots_overlap(other_slot, target) for target in target_slots):
                return signup
    return None


def find_signup_conflicts(schedule):
    """All overlapping confirmed-signup pairs visible in ``schedule``.

    Returns a list of dictionaries, one per overlapping slot pair::

        {
            "attendee": AttendeeProfile,
            "signup_a": AttendeeSignup, "signup_b": AttendeeSignup,
            "submission_a": Submission, "submission_b": Submission,
            "slot_a": TalkSlot, "slot_b": TalkSlot,
        }
    """
    slots = list(participating_slots(schedule))
    if len(slots) < 2:
        return []
    slots_by_submission = _slots_by_submission(slots)
    signups = (
        AttendeeSignup.objects.filter(
            state=AttendeeSignupStates.CONFIRMED,
            submission_id__in=list(slots_by_submission),
        )
        .select_related(
            "attendee",
            "attendee__user",
            "submission",
            "submission__event",
        )
        .order_by("attendee_id", "submission_id")
    )
    signups_by_attendee = defaultdict(list)
    for signup in signups:
        signups_by_attendee[signup.attendee_id].append(signup)

    conflicts = []
    for attendee_signups in signups_by_attendee.values():
        # The (submission, attendee) unique constraint guarantees one
        # signup per session per attendee.
        for index, signup_a in enumerate(attendee_signups):
            for signup_b in attendee_signups[index + 1 :]:
                conflicts.extend(
                    _slot_pair_conflicts(slots_by_submission, signup_a, signup_b)
                )
    return conflicts


def _slot_pair_conflicts(slots_by_submission, signup_a, signup_b):
    slots_a = slots_by_submission.get(signup_a.submission_id, ())
    slots_b = slots_by_submission.get(signup_b.submission_id, ())
    return [
        {
            "attendee": signup_a.attendee,
            "signup_a": signup_a,
            "signup_b": signup_b,
            "submission_a": signup_a.submission,
            "submission_b": signup_b.submission,
            "slot_a": slot_a,
            "slot_b": slot_b,
        }
        for slot_a in slots_a
        for slot_b in slots_b
        if slots_overlap(slot_a, slot_b)
    ]
