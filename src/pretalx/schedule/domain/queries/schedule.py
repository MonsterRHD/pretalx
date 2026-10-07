# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

import datetime as dt

from django.db.models import F

from pretalx.schedule.models import TalkSlot

DAY_START_HOUR = 4


def current_schedule_ordering():
    """Ordering that selects the one current public schedule.

    Confirmed releases are ordered by their monotonic generation, so the
    current schedule is always the confirmed generation with the highest
    number. This fences stale release workers: even if an older generation
    is confirmed (or timestamps collide), it can never become the current
    schedule once a newer generation has been confirmed. The published
    timestamp and primary key only tie-break legacy rows without a
    generation.
    """
    return (F("generation").desc(nulls_last=True), "-published", "-pk")


def confirmed_schedules(event):
    """Schedules of ``event`` that were publicly confirmed.

    Unconfirmed release candidates (version set, ``published`` still NULL)
    are excluded everywhere attendees can reach: web pages, API, feed and
    exports.
    """
    return event.schedules.filter(published__isnull=False)


def published_schedules(event):
    """Confirmed schedules of ``event``, newest generation first, with the
    event preloaded.

    Callers that render the changelog, the Atom feed, or the static HTML
    export all want the same shape: a flat list of all confirmed schedules
    in publication order. Use
    :func:`pretalx.schedule.domain.changelog.build_changelog`
    when ``previous_schedule`` and ``scheduled_talks`` should also be
    batched in.
    """
    return (
        confirmed_schedules(event)
        .select_related("event")
        .order_by(*current_schedule_ordering())
    )


def get_schedule(event, version, *, queryset=None):
    """Look up a schedule by version, or return None.

    Pass a version or the special strings ``"wip"`` or ``"latest"``.
    """
    queryset = event.schedules.all() if queryset is None else queryset
    queryset = queryset.select_related("event")
    if version == "wip":
        return queryset.filter(version__isnull=True).first()
    if version == "latest":
        if not event.current_schedule:
            return None
        return queryset.filter(pk=event.current_schedule.pk).first()
    return queryset.filter(version=version).first()


def public_talk_slots(event):
    """Talk slots visible to non-orga viewers of ``event``.

    Only slots of confirmed schedules qualify; slots of an unconfirmed
    release candidate stay hidden even though their schedule already
    carries a version name.
    """
    return TalkSlot.objects.filter(
        schedule__event=event, is_visible=True, schedule__published__isnull=False
    )


def schedule_day_start(slot):
    local_start = slot.local_start
    day_start = local_start.replace(
        hour=DAY_START_HOUR, minute=0, second=0, microsecond=0
    )
    if local_start.hour < DAY_START_HOUR:
        day_start -= dt.timedelta(days=1)
    return day_start


def _visible_slots(schedule):
    return schedule.talks.filter(
        is_visible=True,
        room__isnull=False,
        start__isnull=False,
        end__isnull=False,
        submission__isnull=False,
    )


def room_neighbour_slots(slot):
    day_start = schedule_day_start(slot)
    same_day_in_room = (
        _visible_slots(slot.schedule)
        .filter(
            room_id=slot.room_id,
            start__gte=day_start,
            start__lt=day_start + dt.timedelta(days=1),
        )
        .exclude(pk=slot.pk)
        .select_related("submission", "submission__event")
    )
    return {
        "previous": same_day_in_room.filter(start__lt=slot.start)
        .order_by("-start")
        .first(),
        "next": same_day_in_room.filter(start__gt=slot.start).order_by("start").first(),
    }


def parallel_slots(slot):
    return (
        _visible_slots(slot.schedule)
        .filter(start__lt=slot.real_end, end__gt=slot.start)
        .exclude(submission_id=slot.submission_id)
        .select_related("submission", "submission__event", "room")
        .order_by("start", "room__position", "room_id")
    )
