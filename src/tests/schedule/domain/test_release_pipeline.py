# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
"""Tests for the durable, generation-based schedule release pipeline."""

import datetime as dt

import pytest
from django.contrib.auth.models import AnonymousUser
from django.utils.timezone import now

from pretalx.mail.models import QueuedMail
from pretalx.schedule.domain.queries.schedule import (
    public_talk_slots,
    published_schedules,
)
from pretalx.schedule.domain.release import (
    ConcurrentReleaseError,
    abort_schedule_release,
    advance_schedule_release,
    build_schedule_release_candidate,
    freeze_schedule,
    recover_schedule_releases,
    unfreeze_schedule,
)
from pretalx.schedule.enums import ScheduleReleaseStage
from pretalx.schedule.models import Schedule, ScheduleRelease
from pretalx.schedule.signals import schedule_release
from pretalx.schedule.tasks import (
    task_advance_schedule_release,
    task_recover_schedule_releases,
)
from pretalx.submission.enums import SubmissionStates
from tests.factories import (
    EventFactory,
    RoomFactory,
    SpeakerFactory,
    SubmissionFactory,
    TalkSlotFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


@pytest.fixture
def released_event():
    """Event with one confirmed submission published as v1."""
    submission = SubmissionFactory(state=SubmissionStates.CONFIRMED)
    event = submission.event
    event.is_public = True
    event.save()
    room = RoomFactory(event=event)
    TalkSlotFactory(
        schedule=event.wip_schedule,
        submission=submission,
        room=room,
        start=event.datetime_from,
        end=event.datetime_from + dt.timedelta(hours=1),
    )
    freeze_schedule(event.wip_schedule, "v1", notify_speakers=False)
    return event


def _new_scheduled_submission(event, *, title=None):
    room = RoomFactory(event=event)
    kwargs = {"event": event, "state": SubmissionStates.CONFIRMED}
    if title is not None:
        kwargs["title"] = title
    submission = SubmissionFactory(**kwargs)
    TalkSlotFactory(
        schedule=event.wip_schedule,
        submission=submission,
        room=room,
        start=event.datetime_from + dt.timedelta(hours=3),
        end=event.datetime_from + dt.timedelta(hours=4),
    )
    return submission


def test_release_record_is_created_with_monotonic_generation(released_event):
    release = build_schedule_release_candidate(
        released_event.wip_schedule, "v2", notify_speakers=False
    )

    assert release.generation == 2
    assert released_event.refresh_from_db() is None
    released_event.refresh_from_db(fields=["schedule_generation"])
    assert released_event.schedule_generation == 2


def test_candidate_is_invisible_on_every_public_surface(released_event):
    event = released_event
    v1 = Schedule.objects.select_related("event").get(pk=event.current_schedule.pk)
    _new_scheduled_submission(event, title="Candidate talk")

    release = build_schedule_release_candidate(
        event.wip_schedule, "v2", notify_speakers=False
    )
    candidate = release.schedule

    # The candidate exists and is complete, but is not public yet.
    assert candidate.version == "v2"
    assert candidate.published is None
    assert candidate.is_candidate is True
    assert candidate.is_released is False
    assert v1.is_released is True

    # The previous generation stays the unique current one.
    assert Schedule.objects.get(pk=event.current_schedule.pk).pk == v1.pk
    assert list(published_schedules(event).values_list("version", flat=True)) == ["v1"]

    # Only v1 slots are public; the candidate slots are hidden.
    public_slots = list(public_talk_slots(event).values_list("submission__title"))
    assert "Candidate talk" not in public_slots

    # The follow-up WIP exists with a copy of the talks.
    assert release.wip_schedule.version is None
    assert release.wip_schedule.talks.count() == candidate.talks.count()

    # The candidate schedule is not viewable by anonymous attendees, while
    # the already confirmed previous generation stays accessible.
    anonymous = AnonymousUser()
    assert anonymous.has_perm("schedule.view_schedule", candidate) is False
    assert anonymous.has_perm("schedule.view_schedule", v1) is True


def test_confirmation_switches_all_surfaces_atomically(released_event):
    event = released_event
    submission = _new_scheduled_submission(event, title="Confirmed talk")

    release = build_schedule_release_candidate(
        event.wip_schedule, "v2", notify_speakers=False
    )

    advance_schedule_release(release.pk)
    release.refresh_from_db()

    assert release.stage == ScheduleReleaseStage.COMPLETE
    assert release.confirmed_at is not None
    assert release.completed_at is not None
    assert Schedule.objects.get(pk=release.schedule_id).published is not None

    # Every surface now serves the same generation.
    assert event.current_schedule.pk == release.schedule_id
    assert list(published_schedules(event).values_list("version", flat=True)) == [
        "v2",
        "v1",
    ]
    public_slots = list(
        public_talk_slots(event).values_list("submission__title", flat=True)
    )
    assert "Confirmed talk" in public_slots

    # The release was logged exactly once, at confirmation.
    assert (
        submission.event.log_entries.filter(
            action_type="pretalx.schedule.release"
        ).count()
        == 2  # v1 and v2
    )


def test_resume_after_crash_picks_up_persisted_candidate(released_event):
    event = released_event
    _new_scheduled_submission(event)
    release = build_schedule_release_candidate(
        event.wip_schedule, "v2", notify_speakers=False
    )

    # Simulate a worker restart: run only the durable task.
    result = task_advance_schedule_release.apply(kwargs={"release_id": release.pk})
    assert result.result == release.pk

    release.refresh_from_db()
    assert release.stage == ScheduleReleaseStage.COMPLETE
    assert event.current_schedule.version == "v2"


def test_recovery_task_and_domain_sweep_resume_candidates(released_event):
    event = released_event
    _new_scheduled_submission(event)
    release = build_schedule_release_candidate(
        event.wip_schedule, "v2", notify_speakers=False
    )

    # Domain-level sweep
    recover_schedule_releases(event)
    release.refresh_from_db()
    assert release.stage == ScheduleReleaseStage.COMPLETE

    # Celery task sweep (no-op, already complete)
    result = task_recover_schedule_releases.apply(kwargs={"event_slug": event.slug})
    assert result.result == event.slug


def test_advance_is_idempotent_for_mails_and_plugins(
    released_event, register_signal_handler
):
    event = released_event
    speaker = SpeakerFactory(event=event)
    submission = _new_scheduled_submission(event)
    submission.speakers.add(speaker)

    calls = []

    def handler(signal, sender, **kwargs):
        calls.append(kwargs)

    register_signal_handler(schedule_release, handler)

    release = build_schedule_release_candidate(
        event.wip_schedule, "v2", notify_speakers=True
    )

    advance_schedule_release(release.pk)
    advance_schedule_release(release.pk)
    advance_schedule_release(release.pk)

    release.refresh_from_db()
    assert release.stage == ScheduleReleaseStage.COMPLETE
    # Exactly one plugin notification, bound to the confirmed generation.
    assert len(calls) == 1
    assert calls[0]["generation"] == release.generation
    assert calls[0]["release"].pk == release.pk
    assert calls[0]["schedule"].pk == release.schedule_id
    # Exactly one speaker draft.
    assert QueuedMail.objects.filter(to_speakers=speaker).count() == 1


def test_notify_false_creates_no_mails_but_completes(released_event):
    event = released_event
    speaker = SpeakerFactory(event=event)
    submission = _new_scheduled_submission(event)
    submission.speakers.add(speaker)

    release = build_schedule_release_candidate(
        event.wip_schedule, "v2", notify_speakers=False
    )
    advance_schedule_release(release.pk)

    assert QueuedMail.objects.filter(to_speakers=speaker).count() == 0
    release.refresh_from_db()
    assert release.stage == ScheduleReleaseStage.COMPLETE
    assert release.notifications_sent_at is not None


def test_failing_plugin_receiver_is_retried_without_duplicating_others(
    released_event, register_signal_handler
):
    event = released_event
    _new_scheduled_submission(event)

    state = {"flaky_calls": 0, "ok_calls": 0}

    def flaky(signal, sender, **kwargs):
        state["flaky_calls"] += 1
        if state["flaky_calls"] == 1:
            raise RuntimeError("plugin boom")

    def always_ok(signal, sender, **kwargs):
        state["ok_calls"] += 1

    register_signal_handler(schedule_release, flaky)
    register_signal_handler(schedule_release, always_ok)

    release = build_schedule_release_candidate(
        event.wip_schedule, "v2", notify_speakers=False
    )

    # First run: public switch already happened; the flaky receiver is recorded.
    advance_schedule_release(release.pk, fail_gracefully=True)
    release.refresh_from_db()
    assert release.confirmed_at is not None
    assert release.stage == ScheduleReleaseStage.CONFIRMED
    assert release.error_data["stage"] == "plugins"
    assert state == {"flaky_calls": 1, "ok_calls": 1}
    assert event.current_schedule.version == "v2"

    # Retry after the worker comes back: flaky succeeds, ok is not re-called.
    advance_schedule_release(release.pk)
    release.refresh_from_db()
    assert release.stage == ScheduleReleaseStage.COMPLETE
    assert state == {"flaky_calls": 2, "ok_calls": 1}
    identities = release.signalled_receivers
    assert len(identities) == 2


def test_freeze_succeeds_publicly_when_plugin_fails_inline(
    released_event, register_signal_handler
):
    event = released_event
    _new_scheduled_submission(event)
    state = {"calls": 0}

    def boom(signal, sender, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("plugin down during the request")

    register_signal_handler(schedule_release, boom)

    # freeze_schedule runs the pipeline inline, but a broken plugin must not
    # roll back or block the already-confirmed public generation.
    released, wip = freeze_schedule(event.wip_schedule, "v2", notify_speakers=False)

    assert released.published is not None
    assert event.current_schedule.version == "v2"
    release = ScheduleRelease.objects.get(schedule=released)
    assert release.confirmed_at is not None
    assert release.stage == ScheduleReleaseStage.CONFIRMED
    assert release.error_data["stage"] == "plugins"

    # The durable retry completes the release once the plugin recovers.
    advance_schedule_release(release.pk)
    release.refresh_from_db()
    assert release.stage == ScheduleReleaseStage.COMPLETE
    assert wip.version is None


def test_older_generation_is_fenced_and_aborted(released_event, monkeypatch):
    event = released_event
    _new_scheduled_submission(event)

    # Two pending generations, as if two workers built candidates and one died.
    r2 = build_schedule_release_candidate(
        event.wip_schedule, "v2", notify_speakers=False
    )
    # Prevent the second build from auto-recovering the first candidate.
    monkeypatch.setattr(
        "pretalx.schedule.domain.release._recover_event_releases", lambda event: None
    )
    # The direct build leaves the test's cached event instance pointing at
    # the old WIP; re-read it (as the next real request would).
    del event.wip_schedule
    r3 = build_schedule_release_candidate(
        event.wip_schedule, "v3", notify_speakers=False
    )

    # The newer generation confirms first.
    advance_schedule_release(r3.pk)
    assert event.current_schedule.version == "v3"

    # The stale older task then runs and must abort instead of overwriting.
    advance_schedule_release(r2.pk)
    r2.refresh_from_db()
    r3.refresh_from_db()
    assert r2.stage == ScheduleReleaseStage.ABORTED
    assert r2.aborted_at is not None
    assert r3.stage == ScheduleReleaseStage.COMPLETE
    assert event.current_schedule.version == "v3"

    # The aborted candidate is gone (version name reusable), follow-up WIP kept.
    assert not Schedule.objects.filter(pk=r2.schedule_id).exists()
    assert Schedule.objects.filter(pk=r3.wip_schedule_id, version__isnull=True).exists()
    assert not event.schedules.filter(version="v2").exists()


def test_aborted_candidate_keeps_previous_public_generation_intact(released_event):
    event = released_event
    _new_scheduled_submission(event)
    r2 = build_schedule_release_candidate(
        event.wip_schedule, "v2", notify_speakers=False
    )

    # Directly abort the unconfirmed candidate (e.g. explicit cancellation).
    abort_schedule_release(r2.pk, reason="cancelled by tests")
    r2.refresh_from_db()

    assert r2.stage == ScheduleReleaseStage.ABORTED
    assert event.current_schedule.version == "v1"
    assert not event.schedules.filter(version="v2").exists()
    # The WIP editors worked on survives, with its talks.
    assert Schedule.objects.filter(version__isnull=True, event=event).count() == 1


def test_build_from_consumed_wip_raises_concurrent_error(released_event):
    event = released_event
    stale_wip = event.wip_schedule
    freeze_schedule(stale_wip, "v2", notify_speakers=False)

    # The caller's instance already shows a versioned schedule; both the
    # upfront validation and the locked identity check refuse the build.
    with pytest.raises((ValueError, ConcurrentReleaseError)):
        build_schedule_release_candidate(stale_wip, "v3", notify_speakers=False)
    # Only the winning release exists.
    assert event.current_schedule.version == "v2"


def test_generation_ordering_wins_over_timestamps(released_event):
    event = released_event
    freeze_schedule(event.wip_schedule, "v2", notify_speakers=False)
    r3 = freeze_schedule(event.wip_schedule, "v3", notify_speakers=False)[0]

    # Rewrite history: make v3's timestamp older than v2's. The generation
    # fence must still keep v3 current.
    Schedule.objects.filter(pk=r3.pk).update(published=now() - dt.timedelta(days=1))
    fresh_event = type(event).objects.get(pk=event.pk)
    assert fresh_event.current_schedule.version == "v3"


def test_unfreeze_recovers_pending_release_then_resets_wip(released_event):
    event = released_event
    v1 = event.schedules.get(version="v1")
    _new_scheduled_submission(event)
    # A release is started but interrupted before confirmation.
    build_schedule_release_candidate(event.wip_schedule, "v2", notify_speakers=False)

    # Withdrawing to v1 first drives the pending generation to confirmation.
    _, new_wip = unfreeze_schedule(v1)

    # The interrupted release converged, current stays the newest generation.
    assert event.current_schedule.version == "v2"
    assert new_wip.version is None


def test_release_before_first_release_has_no_current():
    event = EventFactory()
    assert event.current_schedule is None
    releases = list(
        ScheduleRelease.objects.filter(event=event, stage=ScheduleReleaseStage.COMPLETE)
    )
    assert releases == []
