# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

import itertools

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from pretalx.submission.domain.score_generation import (
    recalculate_submission_review_scores,
    refresh_review_scores,
)


def create_or_update_review(*, submission, user, text, scores=()):
    # Hold the review row across the m2m replacement and the generation
    # fan-out so the batch worker never observes a half-written review.
    with transaction.atomic():
        review, created = submission.reviews.get_or_create(
            user=user, defaults={"text": text}
        )
        # Lock before touching the m2m so the batch worker can never read a
        # half-replaced score selection.
        review = submission.reviews.select_for_update().get(pk=review.pk)
        if not created:
            review.text = text
            review.save()
        review.scores.set(scores)
        return refresh_review_scores(review)


def update_review_score(review):
    """Recompute the confirmed ``review.score`` and pending candidates.

    The confirmed value follows the latest confirmed generation's frozen
    rules (or the live config when no generation exists); pending
    generations receive candidate rows.
    """
    return refresh_review_scores(review)


def recalculate_submission_scores(submission):
    recalculate_submission_review_scores(submission)


def validate_review_phases(event):
    review_phases = list(event.review_phases.all())
    for phase, next_phase in itertools.pairwise(review_phases):
        if not phase.end:
            raise ValidationError(_("Only the last review phase may be open-ended."))
        if not next_phase.start:
            raise ValidationError(
                _("All review phases except for the first one need a start date.")
            )
        if phase.end > next_phase.start:
            raise ValidationError(
                _(
                    "The review phases '{phase1}' and '{phase2}' overlap. "
                    "Please make sure that review phases do not overlap, then save again."
                ).format(phase1=phase.name, phase2=next_phase.name)
            )


def activate_review_phase(phase, *, person=None):
    phase.event.review_phases.update(is_active=False)
    phase.is_active = True
    phase.save()
    phase.log_action(
        ".activate", person=person, orga=person is not None, data={"name": phase.name}
    )


def _is_within_window(phase, _now):
    return (phase.start is None or phase.start <= _now) and (
        phase.end is None or phase.end >= _now
    )


def update_review_phase(event):
    """Advance ``event`` to the next review phase if the current one has
    ended (or has not started yet).

    Returns the now-active phase, or ``None`` when no phase is active.
    """
    _now = now()
    phase = event.active_review_phase
    if phase:
        if _is_within_window(phase, _now):
            return phase
        phase.is_active = False
        phase.save()
    next_phase = next(
        (p for p in event.review_phases.all() if _is_within_window(p, _now)), None
    )
    if next_phase:
        activate_review_phase(next_phase)
        return next_phase
