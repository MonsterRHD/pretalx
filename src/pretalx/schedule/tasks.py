# SPDX-FileCopyrightText: 2025-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

from django_scopes import scope, scopes_disabled

from pretalx.celery_app import app


@app.task(name="pretalx.schedule.update_unreleased_schedule_changes")
def task_update_unreleased_schedule_changes(event=None, value=None):
    from pretalx.event.models import Event  # noqa: PLC0415 -- leaf
    from pretalx.schedule.domain.changes import (  # noqa: PLC0415 -- leaf
        update_unreleased_schedule_changes,
    )

    event = Event.objects.get(slug=event)
    with scope(event=event):
        update_unreleased_schedule_changes(event=event, value=value)


@app.task(
    name="pretalx.schedule.advance_schedule_release",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_backoff_max=60 * 60,
    retry_jitter=True,
    max_retries=20,
    ignore_result=True,
)
def task_advance_schedule_release(self, release_id):
    """Advance a persisted schedule release generation through its stages.

    Every stage is idempotent and concurrency-safe, so this task may run
    repeatedly: inline request handling, this enqueued task and the
    periodic sweeper can all race without double-running any side effect.
    """
    from pretalx.schedule.domain.release import (  # noqa: PLC0415 -- leaf
        advance_schedule_release,
    )
    from pretalx.schedule.models import ScheduleRelease  # noqa: PLC0415 -- leaf

    with scopes_disabled():
        release = (
            ScheduleRelease.objects.select_related("event")
            .filter(pk=release_id)
            .first()
        )
    if not release or release.is_terminal:
        return None
    with scope(event=release.event):
        advance_schedule_release(release_id)
    return release_id


@app.task(
    name="pretalx.schedule.recover_schedule_releases",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=30,
    max_retries=5,
    ignore_result=True,
)
def task_recover_schedule_releases(self, event_slug=None):
    """Resume or abort interrupted schedule releases after worker/process
    restarts. Optionally limited to one event."""
    from pretalx.event.models import Event  # noqa: PLC0415 -- leaf
    from pretalx.schedule.domain.release import (  # noqa: PLC0415 -- leaf
        recover_schedule_releases,
    )
    from pretalx.schedule.enums import ScheduleReleaseStage  # noqa: PLC0415 -- leaf
    from pretalx.schedule.models import ScheduleRelease  # noqa: PLC0415 -- leaf

    with scopes_disabled():
        if event_slug:
            event = Event.objects.filter(slug=event_slug).first()
            if not event:
                return None
            events = [event]
        else:
            event_ids = (
                ScheduleRelease.objects.filter(
                    stage__in=(
                        ScheduleReleaseStage.CANDIDATE,
                        ScheduleReleaseStage.CONFIRMED,
                    )
                )
                .values_list("event_id", flat=True)
                .distinct()
            )
            events = list(Event.objects.filter(pk__in=event_ids))
    for event in events:
        with scope(event=event):
            recover_schedule_releases(event)
    return event_slug or "all"
