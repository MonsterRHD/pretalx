# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

from django.db import models

from pretalx.common.models.managers import ScopedManager
from pretalx.common.models.mixins import TimestampedModel


class ScoreGenerationStatus(models.TextChoices):
    PENDING = "pending", "pending"
    CONFIRMED = "confirmed", "confirmed"
    SUPERSEDED = "superseded", "superseded"


class ScoreGeneration(TimestampedModel):
    """An immutable point-in-time snapshot of one event's scoring rules.

    Consumers read the confirmed generation only (via the denormalised
    ``Review.score`` column). Pending generations receive candidate scores
    while a recalculation is in flight and become visible atomically on
    confirmation.
    """

    event = models.ForeignKey(
        to="event.Event", related_name="score_generations", on_delete=models.CASCADE
    )
    seq = models.PositiveBigIntegerField()
    status = models.CharField(
        max_length=16,
        choices=ScoreGenerationStatus.choices,
        default=ScoreGenerationStatus.PENDING,
    )
    # Resumable keyset cursor over Review ids for the batch worker.
    cursor_position = models.PositiveBigIntegerField(default=0)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    objects = ScopedManager(event="event")

    class Meta:
        ordering = ("seq",)
        unique_together = (("event", "seq"),)
        indexes = [models.Index(fields=("event", "status"))]

    def __str__(self):
        return f"ScoreGeneration(event={self.event_id}, seq={self.seq}, status={self.status})"


class ScoreGenerationCategory(TimestampedModel):
    """Frozen state of one ``ReviewScoreCategory`` at generation creation."""

    generation = models.ForeignKey(
        to=ScoreGeneration, related_name="frozen_categories", on_delete=models.CASCADE
    )
    # Plain integer instead of an FK: later deletion of the live category must
    # not cascade into historical generations.
    source_category_id = models.BigIntegerField()
    weight = models.DecimalField(max_digits=4, decimal_places=1, default=1)
    active = models.BooleanField(default=True)
    is_independent = models.BooleanField(default=False)
    track_ids = models.JSONField(default=list)

    objects = ScopedManager(event="generation__event")

    class Meta:
        unique_together = (("generation", "source_category_id"),)


class ScoreGenerationOption(models.Model):
    """Frozen value of one selectable ``ReviewScore`` at generation creation."""

    category = models.ForeignKey(
        to=ScoreGenerationCategory, related_name="options", on_delete=models.CASCADE
    )
    # Plain integer instead of an FK so deleting the live score option does
    # not destroy the historical snapshot.
    source_score_id = models.BigIntegerField()
    value = models.DecimalField(max_digits=7, decimal_places=2)

    objects = ScopedManager(event="category__generation__event")

    class Meta:
        unique_together = (("category", "source_score_id"),)

    def __str__(self):
        return f"{self.value} (score {self.source_score_id})"


class ReviewScoreCandidate(models.Model):
    """A review's total computed under a not-yet-confirmed generation."""

    generation = models.ForeignKey(
        to=ScoreGeneration, related_name="candidates", on_delete=models.CASCADE
    )
    review = models.ForeignKey(
        to="submission.Review",
        related_name="score_candidates",
        on_delete=models.CASCADE,
    )
    value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Stamp of Review.updated used to detect candidates that online writes left
    # behind after a subsequent review edit.
    review_updated = models.DateTimeField(null=True, blank=True)

    objects = ScopedManager(event="generation__event")

    class Meta:
        unique_together = (("generation", "review"),)
        indexes = [models.Index(fields=("generation", "review_updated"))]

    def __str__(self):
        return f"{self.value} for review {self.review_id} @ generation {self.generation_id}"
