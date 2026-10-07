# SPDX-FileCopyrightText: 2025-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

import logging
from contextlib import suppress

from django.contrib.contenttypes.models import ContentType
from django.db import models, transaction
from django.db.models import Min, Q
from django.db.utils import DatabaseError
from django.utils.timezone import now

from pretalx.common.models.log import ActivityLog
from pretalx.schedule.domain.changes import (
    invalidate_cached_schedule_changes,
    update_unreleased_schedule_changes,
)
from pretalx.schedule.domain.notifications import generate_notifications
from pretalx.schedule.domain.queries.schedule import confirmed_schedules
from pretalx.schedule.domain.slot import copy_slot
from pretalx.schedule.enums import ScheduleReleaseStage, SlotType
from pretalx.schedule.models import Schedule, ScheduleRelease, TalkSlot
from pretalx.schedule.signals import schedule_release
from pretalx.schedule.validators.schedule import validate_version_characters
from pretalx.submission.domain.queries.submission import annotate_requires_signup
from pretalx.submission.enums import SubmissionStates
from pretalx.submission.models import Submission

logger = logging.getLogger(__name__)

PENDING_STAGES = (ScheduleReleaseStage.CANDIDATE, ScheduleReleaseStage.CONFIRMED)


class ConcurrentReleaseError(Exception):
    """Raised when a release is attempted against a WIP schedule that was
    consumed by another, concurrent release while we were waiting on the
    per-event release lock."""


class _AbortReleaseError(Exception):
    """Internal control flow: this candidate can never be confirmed
    (typically because a newer generation already won), so abort it."""


class _RetryReleaseError(Exception):
    """Internal control flow: a retryable post-confirmation stage failed.
    The release stays confirmed and public; the durable task will retry."""


def guess_schedule_version(event):
    if not event.current_schedule:
        return "0.1"

    version = event.current_schedule.version
    prefix = ""
    separator = ""
    for separator in (",", ".", "-", "_"):
        if separator in version:
            prefix, version = version.rsplit(separator, maxsplit=1)
            break
    if version.isdigit():
        version = str(int(version) + 1)
        return prefix + separator + version
    return ""


def freeze_schedule(schedule, name, user=None, notify_speakers=True, comment=None):
    """Freeze a schedule as a new, publicly confirmed version.

    The release runs as a durable, crash-safe generation pipeline: the
    candidate schedule, its visible slots and the follow-up WIP are built
    in one atomic transaction; public surfaces, speaker mails, plugin
    notifications and cache refresh only switch to the new generation once
    it is confirmed. Every post-confirmation step is idempotent and is
    driven both inline (preserving the historical synchronous behaviour)
    and by a durable Celery task that resumes interrupted releases after
    worker or process restarts.
    """
    release = build_schedule_release_candidate(
        schedule, name, user=user, notify_speakers=notify_speakers, comment=comment
    )
    # Preserve the historical synchronous release semantics for callers.
    # The durable task was enqueued on commit; if it wins the race (e.g. in
    # eager mode), every stage here is a no-op.
    advance_schedule_release(release.pk, fail_gracefully=True)

    release = (
        ScheduleRelease.objects.select_related(
            "event", "schedule", "schedule__event", "wip_schedule"
        )
        .prefetch_related("schedule__talks")
        .get(pk=release.pk)
    )
    if release.stage == ScheduleReleaseStage.ABORTED:
        # We were fenced by a newer confirmed generation between build and
        # confirmation. The newer release wins; this one never went public.
        raise ConcurrentReleaseError(
            "The schedule release was superseded by a concurrent newer release."
        )
    if not release.confirmed_at:
        # Confirmation did not complete in-process (e.g. worker dying mid-run).
        # The durable task will resume the persisted candidate, but this call
        # cannot present a confirmed schedule.
        raise ConcurrentReleaseError(
            "The schedule release is still being processed. It will continue "
            "automatically and become available shortly."
        )
    released = release.schedule
    wip_schedule = release.wip_schedule

    # Keep the caller's in-memory schedule instance consistent with the now
    # confirmed generation (callers -- and factory-built related objects --
    # may hold a cached reference to the WIP instance we just froze).
    if released is not None:
        schedule.version = released.version
        schedule.generation = released.generation
        schedule.comment = released.comment
        schedule.published = released.published

    # Drop cached schedule lookups on the caller's event instance, so
    # subsequent access observes the new WIP/current generation.
    with suppress(AttributeError):
        del schedule.event.wip_schedule
    with suppress(AttributeError):
        del schedule.event.current_schedule

    return released, wip_schedule


def build_schedule_release_candidate(
    schedule, name, *, user=None, notify_speakers=True, comment=None
):
    """Build a complete, consistent release candidate in one transaction.

    The candidate (frozen schedule with visible slots plus a fresh
    follow-up WIP) is NOT public yet: it carries its version name but no
    ``published`` timestamp. Nothing attendee-facing selects it until
    :func:`advance_schedule_release` confirms the generation.
    """
    if name in ("wip", "latest"):
        raise ValueError(f'Cannot use reserved name "{name}" for schedule version.')
    if schedule.version:
        raise ValueError(
            f'Cannot freeze schedule version: already versioned as "{schedule.version}".'
        )
    if not name:
        raise ValueError("Cannot create schedule version without a version name.")
    validate_version_characters(name)

    event = schedule.event
    with transaction.atomic():
        # Serialise all releases (and withdrawals) of this event. Together
        # with the generation fence this guarantees that concurrent clicks,
        # republish-after-withdraw and version conflicts converge onto a
        # single current generation.
        locked_event = type(event).objects.select_for_update().get(pk=event.pk)
        wip = (
            Schedule.objects.select_for_update()
            .select_related("event")
            .get(pk=schedule.pk)
        )
        if wip.event_id != locked_event.pk or wip.version or wip.published:
            raise ConcurrentReleaseError(
                "The schedule is no longer the current WIP schedule; a concurrent "
                "release has advanced it."
            )

        # Resolve any interrupted releases before starting a new generation.
        _recover_event_releases(locked_event)

        locked_event.schedule_generation = models.F("schedule_generation") + 1
        locked_event.save(update_fields=["schedule_generation", "updated"])
        locked_event.refresh_from_db(fields=["schedule_generation"])
        generation = locked_event.schedule_generation

        wip.version = name
        wip.comment = comment
        wip.generation = generation
        wip.save(update_fields=["generation", "version", "comment", "updated"])

        # Confirmed submissions and breaks are visible; blockers stay hidden.
        wip.talks.update(is_visible=False)
        wip.talks.filter(
            models.Q(submission__state=SubmissionStates.CONFIRMED)
            | models.Q(slot_type=SlotType.BREAK),
            start__isnull=False,
        ).update(is_visible=True)

        wip_schedule = locked_event.schedules.create()
        talks = [
            copy_slot(talk, schedule=wip_schedule, save=False)
            for talk in wip.talks.select_related("submission", "room").all()
        ]
        TalkSlot.objects.bulk_create(talks)

        # Blockers should only exist in WIP, never in a released schedule.
        wip.talks.filter(slot_type=SlotType.BLOCKER).delete()

        apply_signup_capacity_defaults(wip, user=user)

        release = ScheduleRelease.objects.create(
            event=locked_event,
            schedule=wip,
            wip_schedule=wip_schedule,
            generation=generation,
            user=user,
            notify_speakers=notify_speakers,
            stage=ScheduleReleaseStage.CANDIDATE,
        )

        # Durable continuation: if this process dies between committing the
        # candidate and confirming it, a worker picks the release up. All
        # stages are idempotent, so a double run (inline + task) is safe.
        transaction.on_commit(lambda: _enqueue_advance(release.pk))

    return release


def _enqueue_advance(release_id):
    try:
        from pretalx.schedule.tasks import (  # noqa: PLC0415 -- leaf import
            task_advance_schedule_release,
        )

        task_advance_schedule_release.apply_async(
            kwargs={"release_id": release_id}, ignore_result=True
        )
    except Exception:  # pragma: no cover -- broker outage must not break the release
        logger.exception(
            "Could not enqueue schedule release task for release %s; "
            "the periodic recovery task will resume it.",
            release_id,
        )


def _locked_release(release_id):
    return (
        ScheduleRelease.objects.select_for_update()
        .select_related("event", "schedule", "schedule__event", "wip_schedule", "user")
        .get(pk=release_id)
    )


def advance_schedule_release(release_id, *, fail_gracefully=False):
    """Drive a release through every pending stage.

    Idempotent and safe to call concurrently (inline request + Celery
    worker + recovery sweep): each stage takes a row lock and records its
    completion before the next one starts. Returns the refreshed
    ScheduleRelease, or None if it was deleted.
    """
    if not ScheduleRelease.objects.filter(pk=release_id).exists():
        return None
    try:
        _stage_confirm(release_id)
        _stage_notifications(release_id)
        _stage_plugins(release_id)
        _stage_cache_refresh(release_id)
        _stage_complete(release_id)
    except _AbortReleaseError as exc:
        logger.warning("Aborting schedule release %s: %s", release_id, exc)
        abort_schedule_release(release_id, reason=str(exc))
    except Exception:
        logger.exception("Error while advancing schedule release %s", release_id)
        if not fail_gracefully:
            raise
    return ScheduleRelease.objects.filter(pk=release_id).first()


def _stage_confirm(release_id):
    """Switch every public surface atomically by setting ``published``.

    Before this point the candidate exists but is invisible; afterwards it
    is the current generation for web pages, API, feed, exports and caches.
    """
    with transaction.atomic():
        release = _locked_release(release_id)
        if release.is_terminal or release.confirmed_at:
            return
        candidate = release.schedule
        if candidate is None or candidate.published is not None:
            release.confirmed_at = candidate.published if candidate else now()
            release.stage = ScheduleReleaseStage.CONFIRMED
            release.save(update_fields=["stage", "confirmed_at", "updated"])
            return

        # Generation fence: an older, stale task must never publish over a
        # newer generation that is already confirmed.
        newer_confirmed = (
            confirmed_schedules(release.event)
            .filter(generation__gt=release.generation)
            .exists()
        )
        if newer_confirmed:
            raise _AbortReleaseError(
                f"generation {release.generation} is superseded by a newer confirmed release"
            )

        timestamp = now()
        candidate.published = timestamp
        candidate.save(update_fields=["published", "updated"])
        release.confirmed_at = timestamp
        release.stage = ScheduleReleaseStage.CONFIRMED
        release.save(update_fields=["stage", "confirmed_at", "updated"])
        candidate.log_action("pretalx.schedule.release", person=release.user, orga=True)


def _stage_notifications(release_id):
    """Create speaker notification drafts, bound to the confirmed schedule.

    The whole stage is one transaction and its completion marker is set in
    the same transaction, so a crash either fully creates the drafts plus
    marker or neither: a retry can never produce duplicate mails.
    """
    with transaction.atomic():
        release = _locked_release(release_id)
        if release.is_terminal or release.notifications_sent_at:
            return
        if release.confirmed_at and release.notify_speakers:
            schedule = Schedule.objects.select_related("event", "event__cfp").get(
                pk=release.schedule_id
            )
            generate_notifications(schedule)
        release.notifications_sent_at = now()
        release.save(update_fields=["notifications_sent_at", "updated"])


def _receiver_identity(receiver):
    return f"{getattr(receiver, '__module__', '?')}.{getattr(receiver, '__qualname__', getattr(receiver, '__name__', '?'))}"


def _stage_plugins(release_id):
    """Notify ``schedule_release`` plugin receivers, exactly once each.

    Every receiver is invoked in its own savepoint together with recording
    its success, so a crash or a failing receiver only ever delays (never
    duplicates) a successful notification. Receivers additionally receive
    the ``release`` and ``generation`` keyword arguments so they can
    deduplicate their own external side effects.
    """
    release = ScheduleRelease.objects.select_related(
        "event", "schedule", "schedule__event", "user"
    ).get(pk=release_id)
    if release.is_terminal or release.plugins_notified_at:
        return

    event = release.event
    candidate = release.schedule
    active_receivers = []
    with suppress(IndexError, AttributeError, TypeError):
        active_receivers = schedule_release.get_active_receivers(event)
    done = set(release.signalled_receivers or [])
    failures = []
    for receiver in active_receivers:
        identity = _receiver_identity(receiver)
        if identity in done:
            continue
        try:
            with transaction.atomic():
                locked = _locked_release(release_id)
                if identity in set(locked.signalled_receivers or []):
                    continue
                if locked.aborted_at:
                    return
                receiver(
                    signal=schedule_release,
                    sender=event,
                    schedule=candidate,
                    user=release.user,
                    release=locked,
                    generation=release.generation,
                )
                locked.signalled_receivers = [
                    *(locked.signalled_receivers or []),
                    identity,
                ]
                locked.save(update_fields=["signalled_receivers", "updated"])
        except Exception as exc:  # noqa: BLE001 -- per-receiver isolation
            failures.append(f"{identity}: {type(exc).__name__}: {exc}")

    with transaction.atomic():
        locked = _locked_release(release_id)
        if failures:
            locked.error_data = {"stage": "plugins", "failures": failures[:20]}
            locked.error_timestamp = now()
            locked.save(update_fields=["error_data", "error_timestamp", "updated"])
        elif not locked.plugins_notified_at:
            locked.plugins_notified_at = now()
            update_fields = ["plugins_notified_at", "updated"]
            if locked.error_data:
                locked.error_data = None
                locked.error_timestamp = None
                update_fields += ["error_data", "error_timestamp"]
            locked.save(update_fields=update_fields)

    if failures:
        raise _RetryReleaseError("plugin receivers failed: " + "; ".join(failures[:3]))


def _stage_cache_refresh(release_id):
    """Refresh all event-scoped public caches to the confirmed generation.

    Cache operations are best effort and inherently idempotent: failure is
    recorded but never blocks completion, since cached entries expire on
    their own and every cache key is generation- or version-scoped.
    """
    release = ScheduleRelease.objects.select_related("event", "wip_schedule").get(
        pk=release_id
    )
    if release.is_terminal or release.cache_refreshed_at:
        return
    if not release.confirmed_at:
        return

    event = release.event
    wip = (
        Schedule.objects.select_related("event")
        .filter(pk=release.wip_schedule_id)
        .first()
    )
    operations = []
    # Only the fresh WIP schedule needs change-cache invalidation. The
    # confirmed schedule's changes cache was populated (e.g. by the speaker
    # notifications) against the same previous generation and stays valid.
    if wip is not None:
        operations.append(
            (
                f"invalidate changes {wip.pk}",
                lambda: invalidate_cached_schedule_changes(wip),
            )
        )
    operations.append(
        (
            "reset unreleased changes flag",
            lambda: update_unreleased_schedule_changes(event, False),
        )
    )
    errors = []
    for description, operation in operations:
        try:
            operation()
        except Exception:  # noqa: BLE001 -- cache backends must never block releases
            logger.exception(
                "Schedule release cache refresh step failed: %s", description
            )
            errors.append(description)
    with transaction.atomic():
        locked = _locked_release(release_id)
        if not locked.cache_refreshed_at:
            locked.cache_refreshed_at = now()
            update_fields = ["cache_refreshed_at", "updated"]
            if errors:
                locked.error_data = {"stage": "cache", "failures": errors}
                locked.error_timestamp = now()
                update_fields += ["error_data", "error_timestamp"]
            locked.save(update_fields=update_fields)


def _stage_complete(release_id):
    with transaction.atomic():
        release = _locked_release(release_id)
        if release.stage == ScheduleReleaseStage.COMPLETE:
            return
        if release.aborted_at or not release.confirmed_at:
            return
        # All post-confirmation side effects must have recorded completion.
        # (The plugins stage raises _RetryReleaseError on failure, which
        # prevents us from reaching this point.)
        if not (
            release.notifications_sent_at
            and release.plugins_notified_at
            and release.cache_refreshed_at
        ):
            return
        release.stage = ScheduleReleaseStage.COMPLETE
        release.completed_at = now()
        release.save(update_fields=["stage", "completed_at", "updated"])


def abort_schedule_release(release_id, *, reason=""):
    """Roll back an unconfirmed candidate release.

    The previous public generation is untouched (the candidate never
    became public). The follow-up WIP remains the editors' working copy,
    and the candidate schedule, its slots and its version name are
    discarded so the version can be reused.
    """
    with transaction.atomic():
        _event = (
            ScheduleRelease.objects.select_related("event")
            .values_list("event_id", flat=True)
            .get(pk=release_id)
        )
        # Take the event lock so this cannot interleave with a fresh build.
        from pretalx.event.models import Event  # noqa: PLC0415 -- leaf import

        Event.objects.select_for_update().get(pk=_event)
        release = _locked_release(release_id)
        if release.stage == ScheduleReleaseStage.ABORTED:
            return release
        if release.confirmed_at:
            # A confirmed generation cannot be un-published by aborting;
            # finish it instead.
            raise RuntimeError("Cannot abort an already confirmed schedule release")

        candidate = release.schedule
        if candidate and not candidate.published:
            TalkSlot.objects.filter(schedule=candidate).delete()
            candidate.delete()
            release.schedule = None

        release.stage = ScheduleReleaseStage.ABORTED
        release.aborted_at = now()
        release.error_data = {"abort_reason": reason}
        release.error_timestamp = now()
        release.save(
            update_fields=[
                "schedule",
                "stage",
                "aborted_at",
                "error_data",
                "error_timestamp",
                "updated",
            ]
        )
    return release


def _recover_event_releases(event):
    """Advance all interrupted releases of one event.

    Called under the event release lock (during build/withdraw) and by the
    recovery sweeper. Older generations first; the generation fence makes
    superseded candidates abort themselves.
    """
    pending_ids = list(
        event.schedule_releases.filter(stage__in=PENDING_STAGES)
        .order_by("generation")
        .values_list("pk", flat=True)
    )
    for release_id in pending_ids:
        advance_schedule_release(release_id, fail_gracefully=True)


def recover_schedule_releases(event=None):
    """Resume or abort all interrupted releases, optionally for one event.

    This is the restart-safe entry point used by the Celery recovery task
    and the periodic maintenance command.
    """
    queryset = ScheduleRelease.objects.filter(stage__in=PENDING_STAGES).order_by(
        "event_id", "generation"
    )
    if event is not None:
        queryset = queryset.filter(event=event)
    for release_id in queryset.values_list("pk", flat=True):
        advance_schedule_release(release_id, fail_gracefully=True)


def apply_signup_capacity_defaults(schedule, user=None):
    """Set the session capacity to the room capacity.

    Runs on non-visible sessions too, so that warnings and other integrations
    like expansion on room update work.
    """
    if not schedule.event.get_feature_flag("attendee_signup"):
        return
    scheduled_in_schedule = Q(
        slots__schedule=schedule,
        slots__room__capacity__isnull=False,
        slots__start__isnull=False,
    )
    qs = annotate_requires_signup(
        Submission.objects.filter(
            scheduled_in_schedule, attendee_signup_capacity__isnull=True
        )
        .select_related("track", "submission_type")
        .annotate(
            room_capacity=Min("slots__room__capacity", filter=scheduled_in_schedule)
        )
        .distinct()
    ).filter(_annotated_requires_signup=True)
    updates = [(submission, submission.room_capacity) for submission in qs]
    apply_signup_capacity_changes(schedule.event, updates, user=user)


@transaction.atomic
def apply_signup_capacity_changes(event, updates, user=None):
    if not updates:
        return
    submission_ct = ContentType.objects.get_for_model(Submission)
    timestamp = now()
    to_update = []
    log_entries = []
    for submission, new_capacity in updates:
        old_capacity = submission.attendee_signup_capacity
        if old_capacity == new_capacity:
            continue
        submission.attendee_signup_capacity = new_capacity
        submission.updated = timestamp
        to_update.append(submission)
        log_entries.append(
            ActivityLog(
                event=event,
                person=user,
                content_type=submission_ct,
                object_id=submission.pk,
                action_type="pretalx.submission.update",
                is_orga_action=True,
                data={
                    "changes": {
                        "attendee_signup_capacity": {
                            "old": old_capacity,
                            "new": new_capacity,
                        }
                    }
                },
            )
        )
    if not to_update:
        return
    Submission.objects.bulk_update(to_update, ["attendee_signup_capacity", "updated"])
    ActivityLog.objects.bulk_create(log_entries)


def unfreeze_schedule(schedule, user=None):
    """Resets the current WIP schedule to an older schedule version."""
    if not schedule.version:
        raise ValueError("Cannot unfreeze schedule version: not released yet.")
    if not schedule.published:
        raise ValueError(
            "Cannot unfreeze schedule version: this generation was not confirmed."
        )

    submission_ids = schedule.talks.values_list("submission_id", flat=True)
    event = schedule.event

    with transaction.atomic():
        # Serialise against concurrent releases and make sure no interrupted
        # generation is left dangling while we replace the WIP.
        from pretalx.event.models import Event  # noqa: PLC0415 -- leaf import

        locked_event = Event.objects.select_for_update().get(pk=event.pk)
        _recover_event_releases(locked_event)

        talks = locked_event.wip_schedule.talks.exclude(
            submission_id__in=submission_ids
        )
        try:
            # Force evaluation to catch the DatabaseError early.
            talks = list(talks.union(schedule.talks.all()))
        except DatabaseError:  # pragma: no cover -- vendor-specific SQLite workaround
            talks = set(talks) | set(schedule.talks.all())

        wip_schedule = locked_event.schedules.create()
        new_talks = [
            copy_slot(talk, schedule=wip_schedule, save=False) for talk in talks
        ]
        TalkSlot.objects.bulk_create(new_talks)

        locked_event.wip_schedule.talks.all().delete()
        locked_event.wip_schedule.delete()

    update_unreleased_schedule_changes(event, False)

    with suppress(AttributeError):
        del wip_schedule.event.wip_schedule

    return schedule, wip_schedule
