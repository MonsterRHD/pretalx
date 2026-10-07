# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-TERMS

from django.db import models
from django.utils.translation import gettext_lazy as _

from pretalx.common.models.mixins import PretalxModel
from pretalx.schedule.enums import ScheduleReleaseStage


class ScheduleRelease(PretalxModel):
    """One durable, crash-safe schedule release operation ("generation").

    The row is created in the same transaction that builds the candidate
    schedule (frozen WIP with visible slots plus a fresh successor WIP) and
    tracks every subsequent stage of the release. Public surfaces only
    switch to ``schedule`` once :attr:`confirmed_at` is set, and every
    post-confirmation side effect (speaker mails, plugin notifications,
    cache refresh) records its completion here so that it can be retried
    idempotently by workers after a crash.

    ``generation`` is a monotonic per-event counter that fences stale
    workers: a release task belonging to an older generation can never
    overwrite a newer generation that has already been confirmed.
    """

    event = models.ForeignKey(
        to="event.Event",
        on_delete=models.PROTECT,
        related_name="schedule_releases",
    )
    schedule = models.ForeignKey(
        to="schedule.Schedule",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="release",
        help_text=_("The candidate/current schedule produced by this release."),
    )
    wip_schedule = models.ForeignKey(
        to="schedule.Schedule",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_release",
        help_text=_("The follow-up WIP schedule created by this release."),
    )
    generation = models.PositiveBigIntegerField(
        verbose_name=_("Generation"),
        help_text=_(
            "Monotonic per-event generation number used to fence stale release tasks."
        ),
    )
    user = models.ForeignKey(
        to="person.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="schedule_releases",
    )
    notify_speakers = models.BooleanField(default=False)
    stage = models.CharField(
        max_length=20,
        choices=ScheduleReleaseStage.choices,
        default=ScheduleReleaseStage.CANDIDATE,
        verbose_name=_("Stage"),
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    notifications_sent_at = models.DateTimeField(null=True, blank=True)
    plugins_notified_at = models.DateTimeField(null=True, blank=True)
    cache_refreshed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    aborted_at = models.DateTimeField(null=True, blank=True)
    # Identities (``module.qualname``) of plugin receivers already notified,
    # so a retried release never calls a successful receiver twice.
    signalled_receivers = models.JSONField(default=list, blank=True)
    # Last recoverable error encountered while advancing the release.
    error_data = models.JSONField(null=True, blank=True)
    error_timestamp = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = _("Schedule releases")
        ordering = ("-generation",)
        constraints = [
            models.UniqueConstraint(
                fields=["event", "generation"],
                name="schedule_release_unique_generation_per_event",
            )
        ]

    def __str__(self) -> str:
        version = getattr(self.schedule, "version", None) or "candidate"
        return f"ScheduleRelease(event={self.event.slug}, generation={self.generation}, version={version})"

    @property
    def is_terminal(self) -> bool:
        return self.stage in (ScheduleReleaseStage.COMPLETE, ScheduleReleaseStage.ABORTED)

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None
