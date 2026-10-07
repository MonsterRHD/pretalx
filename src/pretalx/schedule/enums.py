# SPDX-FileCopyrightText: 2017-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

from django.db import models
from django.utils.translation import gettext_lazy as _


class SlotType(models.TextChoices):
    BREAK = "break", _("Break")
    BLOCKER = "blocker", _("Blocker")


class ScheduleReleaseStage(models.TextChoices):
    """Persistent state machine stages for a single schedule release.

    A release starts as a ``CANDIDATE``: the frozen schedule, its visible
    slots and the follow-up WIP all exist in the database, but the candidate
    carries no ``published`` timestamp and is invisible on every public
    surface. It becomes publicly current only in ``CONFIRMED``. The
    remaining stages are post-confirmation side effects that can be retried
    idempotently; ``COMPLETE`` is the happy terminal state, ``ABORTED`` the
    rollback terminal state (only reachable before confirmation).
    """

    CANDIDATE = "candidate", _("Candidate")
    CONFIRMED = "confirmed", _("Confirmed")
    COMPLETE = "complete", _("Complete")
    ABORTED = "aborted", _("Aborted")
