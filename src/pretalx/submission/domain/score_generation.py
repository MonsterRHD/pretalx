# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

"""Persistent scoring generations for review score recalculation.

A score generation freezes the scoring rules in force at one settings commit
(category weights, active/independent flags, track restrictions and score
option values). Recalculation writes :class:`ReviewScoreCandidate` rows for a
pending generation while every consumer keeps reading the denormalised
``Review.score`` column, which always reflects the confirmed generation. A
generation becomes visible through one atomic confirmation transaction.
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from django.db import OperationalError, transaction
from django.db.models import F, Max, Q
from django.utils.timezone import now

from pretalx.submission.models import (
    Review,
    ReviewScoreCandidate,
    ScoreGeneration,
    ScoreGenerationCategory,
    ScoreGenerationOption,
    ScoreGenerationStatus,
)

LOGGER = logging.getLogger(__name__)

PENDING = ScoreGenerationStatus.PENDING
CONFIRMED = ScoreGenerationStatus.CONFIRMED
SUPERSEDED = ScoreGenerationStatus.SUPERSEDED


@dataclass(frozen=True)
class FrozenCategory:
    source_category_id: int
    weight: Decimal
    active: bool
    is_independent: bool
    track_ids: frozenset


@dataclass(frozen=True)
class FrozenRules:
    """In-memory view of one generation's frozen rules.

    ``options`` maps a live ``ReviewScore`` id to
    ``(source category id, frozen value)``.
    """

    generation_id: int
    categories: dict
    options: dict


def load_frozen_rules(generation) -> FrozenRules:
    """Load a generation snapshot with a fixed number of queries."""
    categories = {
        row.source_category_id: FrozenCategory(
            source_category_id=row.source_category_id,
            weight=row.weight,
            active=row.active,
            is_independent=row.is_independent,
            track_ids=frozenset(row.track_ids or ()),
        )
        for row in generation.frozen_categories.all()
    }
    options = {
        option.source_score_id: (option.category.source_category_id, option.value)
        for option in ScoreGenerationOption.objects.filter(
            category__generation=generation
        ).select_related("category")
    }
    return FrozenRules(
        generation_id=generation.pk, categories=categories, options=options
    )


def freeze_score_settings(event) -> list[dict]:
    """Collect the event's current live scoring rules for persistence."""
    return [
        {
            "source_category_id": category.id,
            "weight": category.weight,
            "active": category.active,
            "is_independent": category.is_independent,
            "track_ids": sorted(track.id for track in category.limit_tracks.all()),
            "options": [
                {"source_score_id": score.id, "value": score.value}
                for score in category.scores.all()
            ],
        }
        for category in event.score_categories.prefetch_related(
            "scores", "limit_tracks"
        ).order_by("id")
    ]


def _persist_freeze(generation, frozen: list[dict]) -> None:
    frozen_categories = [
        ScoreGenerationCategory(
            generation=generation,
            source_category_id=item["source_category_id"],
            weight=item["weight"],
            active=item["active"],
            is_independent=item["is_independent"],
            track_ids=item["track_ids"],
        )
        for item in frozen
    ]
    ScoreGenerationCategory.objects.bulk_create(frozen_categories)

    frozen_options = []
    for item, frozen_category in zip(frozen, frozen_categories, strict=True):
        frozen_options.extend(
            ScoreGenerationOption(
                category=frozen_category,
                source_score_id=option["source_score_id"],
                value=option["value"],
            )
            for option in item["options"]
        )
    ScoreGenerationOption.objects.bulk_create(frozen_options)


def confirmed_generation(event):
    return event.score_generations.filter(status=CONFIRMED).order_by("-seq").first()


def pending_generations(event):
    return list(event.score_generations.filter(status=PENDING).order_by("seq"))


def ensure_baseline_generation(event):
    """Create a confirmed generation for events without any (tests/edge cases).

    Production events receive their baseline through the 0114 data migration.
    Returns the new generation, or ``None`` when one already existed.
    """
    if event.score_generations.exists():
        return None
    generation = ScoreGeneration.objects.create(
        event=event, seq=1, status=CONFIRMED, confirmed_at=now()
    )
    _persist_freeze(generation, freeze_score_settings(event))
    return generation


def commit_score_generation(event) -> ScoreGeneration:
    """Freeze the just-saved live settings as a new pending generation.

    Supersedes any previously pending generation and reserves the next seq.
    Callers must dispatch the recalculation task after the surrounding
    transaction commits.
    """
    with transaction.atomic():
        locked = type(event).objects.select_for_update().get(pk=event.pk)
        ensure_baseline_generation(locked)
        current_max = (
            locked.score_generations.aggregate(current=Max("seq")).get("current") or 0
        )
        locked.score_generations.filter(status=PENDING).update(status=SUPERSEDED)
        generation = ScoreGeneration.objects.create(
            event=locked, seq=current_max + 1, status=PENDING
        )
        _persist_freeze(generation, freeze_score_settings(locked))
        return generation


def _selected_scores(review):
    """Return ``(score option id, source category id)`` for a review."""
    return [(score.id, score.category_id) for score in review.scores.all()]


def score_review_under_rules(review, rules: FrozenRules):
    """Compute a review's weighted total against frozen generation rules.

    Applicability mirrors the legacy live formula: the category must be
    active and track-matching. Independent categories participate (their
    frozen weight is 0, matching ``ReviewScoreCategory.save``), so a review
    that only selected an independent option keeps the historical total of 0
    rather than becoming unrated. Returns ``None`` when no selected option is
    applicable (abstention or all options inactive/track-excluded).
    """
    track_id = review.submission.track_id
    total = None
    for score_id, category_id in _selected_scores(review):
        option = rules.options.get(score_id)
        if option is None or option[0] != category_id:
            # Option unknown to this generation or relocated: ignore.
            continue
        category = rules.categories.get(category_id)
        if category is None or not category.active:
            continue
        if category.track_ids and track_id not in category.track_ids:
            continue
        contribution = option[1] * category.weight
        total = contribution if total is None else total + contribution
    return total


def score_review_under_generation(review, generation):
    return score_review_under_rules(review, load_frozen_rules(generation))


def _score_review_live(review):
    """Legacy scoring from current live config; used when no generation exists."""
    scores = list(
        review.scores.select_related("category").filter(
            category__in=review.submission.score_categories
        )
    )
    return (
        sum(score.value * score.category.weight for score in scores) if scores else None
    )


def write_candidate(review, generation, *, rules=None) -> bool:
    """Upsert a review's candidate for one pending generation.

    No-op for superseded or confirmed generations, so a late worker or an
    online write racing a confirmation never revives stale candidates.
    """
    if generation.status != PENDING:
        return False
    rules = rules or load_frozen_rules(generation)
    value = score_review_under_rules(review, rules)
    ReviewScoreCandidate.objects.update_or_create(
        generation=generation,
        review=review,
        defaults={"value": value, "review_updated": review.updated},
    )
    return True


def _refresh_once(review) -> Review:
    with transaction.atomic():
        locked = (
            Review.objects.select_for_update()
            .select_related("submission__event", "submission__track", "user")
            .prefetch_related("scores")
            .get(pk=review.pk)
        )
        event_id = locked.submission.event_id
        # Lock only PENDING generations. Locking confirmed rows too would hold
        # a lower-seq row while waiting for a higher one, which inverts against
        # confirmation's final status updates (deadlock on PostgreSQL). The
        # single shared lock direction is: review first, pending generation(s)
        # in seq order afterwards -- matching the batch worker and
        # confirmation (which locks all reviews before the generation).
        pending = list(
            ScoreGeneration.objects.select_for_update()
            .filter(event_id=event_id, status=PENDING)
            .order_by("seq")
        )
        # Read confirmed state only after the pending locks are held: a
        # confirmation racing us either committed before this statement (fully
        # visible) or still holds the pending lock (we are blocked and
        # re-evaluate after it commits). We can therefore never publish a score
        # computed under a generation that is being retired concurrently.
        confirmed = (
            ScoreGeneration.objects.filter(event_id=event_id, status=CONFIRMED)
            .order_by("-seq")
            .first()
        )
        if confirmed is None:
            value = _score_review_live(locked)
        else:
            value = score_review_under_rules(locked, load_frozen_rules(confirmed))
        # QuerySet.update avoids bumping Review.updated: that timestamp marks
        # content edits and anchors the candidate freshness stamps.
        Review.objects.filter(pk=locked.pk).update(score=value)
        locked.score = value
        # Keep the caller's in-memory instance in sync (serializers return it).
        review.score = value

        for generation in pending:
            write_candidate(locked, generation)
    return locked


def _is_deadlock(error) -> bool:
    return getattr(getattr(error, "orig", None), "pgcode", None) == "40P01"


def refresh_review_scores(review) -> Review:
    """Recompute the confirmed ``Review.score`` and every pending candidate.

    The confirmed value uses the latest confirmed generation's frozen rules;
    pending generations receive fresh candidate rows. Everything happens in
    one transaction holding the review row lock. The extremely narrow
    confirm/review lock-inversion window PostgreSQL resolves by aborting one
    transaction (SQLSTATE 40P01) is retried once after a short backoff; the
    retry reads the winner's committed state.
    """
    try:
        return _refresh_once(review)
    except OperationalError as error:
        if not _is_deadlock(error):
            raise
        LOGGER.warning(
            "Deadlock while refreshing review %s scores; retrying once.",
            review.pk,
        )
        time.sleep(0.1)
        return _refresh_once(review)


def recalculate_submission_review_scores(submission) -> None:
    """Immediate fan-out for one submission's reviews (e.g. track change)."""
    for review in submission.reviews.all():
        refresh_review_scores(review)


# ---------------------------------------------------------------------------
# Batch worker
# ---------------------------------------------------------------------------

CANDIDATE_BATCH_SIZE = 500


def _lock_event_reviews(event_id) -> list[int]:
    """Lock all of an event's review rows in pk order.

    Taking review locks before the generation lock establishes one global
    lock order (event -> review -> generation) shared with online writers,
    eliminating lock-inversion deadlocks on PostgreSQL.
    """
    return list(
        Review.objects.select_for_update()
        .filter(submission__event_id=event_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def process_generation_batch(generation) -> str:
    """Compute one cursor batch of missing candidates.

    Returns ``"progress"`` when a batch was written, ``"caught_up"`` when no
    review beyond the persisted cursor lacks a candidate, and ``"stale"`` for
    generations that are no longer pending. The batch is one transaction, so a
    killed worker resumes from persisted progress on its next invocation.
    """
    current = ScoreGeneration.objects.filter(pk=generation.pk).first()
    if current is None or current.status != PENDING:
        return "stale"
    review_ids = list(
        Review.objects.filter(
            submission__event_id=current.event_id, pk__gt=current.cursor_position
        )
        .exclude(score_candidates__generation=current)
        .order_by("pk")
        .values_list("pk", flat=True)[:CANDIDATE_BATCH_SIZE]
    )
    if not review_ids:
        return "caught_up"

    with transaction.atomic():
        # Reviews first, then the generation row: this matches online writers'
        # lock order (review -> generation) and prevents lock inversion.
        reviews = list(
            Review.objects.select_for_update()
            .select_related("submission")
            .prefetch_related("scores")
            .filter(pk__in=review_ids)
            .order_by("pk")
        )
        locked = ScoreGeneration.objects.select_for_update().get(pk=generation.pk)
        if locked.status != PENDING:
            return "stale"
        # Online writers may have provisioned candidates between the plain
        # read and the locks; never overwrite their fresher rows.
        existing = set(
            ReviewScoreCandidate.objects.filter(
                generation=locked, review__in=reviews
            ).values_list("review_id", flat=True)
        )
        rules = load_frozen_rules(locked)
        ReviewScoreCandidate.objects.bulk_create(
            [
                ReviewScoreCandidate(
                    generation=locked,
                    review=review,
                    value=score_review_under_rules(review, rules),
                    review_updated=review.updated,
                )
                for review in reviews
                if review.pk not in existing
            ],
            ignore_conflicts=True,
        )
        locked.cursor_position = max(review_ids)
        locked.save(update_fields=["cursor_position"])
        return "progress"


def _gap_review_ids(generation) -> list[int]:
    """Reviews missing a candidate, or whose candidate is older than the review."""
    missing = Review.objects.filter(submission__event_id=generation.event_id).exclude(
        score_candidates__generation=generation
    )
    stale = Review.objects.filter(score_candidates__generation=generation).filter(
        Q(score_candidates__review_updated__lt=F("updated"))
        | Q(score_candidates__review_updated__isnull=True, updated__isnull=False)
    )
    ids = set(missing.values_list("pk", flat=True))
    ids.update(stale.values_list("pk", flat=True))
    return sorted(ids)


def sweep_generation(generation) -> int:
    """Event-wide backfill for missing or stale candidates (final safety net)."""
    current = ScoreGeneration.objects.filter(pk=generation.pk).first()
    if current is None or current.status != PENDING:
        return 0
    review_ids = _gap_review_ids(current)
    if not review_ids:
        return 0

    with transaction.atomic():
        # Same lock order as the batch worker: reviews then generation.
        locked_reviews = list(
            Review.objects.select_for_update()
            .select_related("submission")
            .prefetch_related("scores")
            .filter(pk__in=review_ids)
            .order_by("pk")
        )
        locked = ScoreGeneration.objects.select_for_update().get(pk=generation.pk)
        if locked.status != PENDING:
            return 0
        # The gap set may have changed concurrently; recompute on locked rows.
        gap_ids = set(_gap_review_ids(locked))
        rules = load_frozen_rules(locked)
        targets = [review for review in locked_reviews if review.pk in gap_ids]
        for review in targets:
            ReviewScoreCandidate.objects.update_or_create(
                generation=locked,
                review=review,
                defaults={
                    "value": score_review_under_rules(review, rules),
                    "review_updated": review.updated,
                },
            )
        return len(targets)


def generation_is_complete(generation) -> bool:
    """True iff one fresh candidate exists per current review, NULLs included."""
    review_count = Review.objects.filter(
        submission__event_id=generation.event_id
    ).count()
    candidate_count = ReviewScoreCandidate.objects.filter(generation=generation).count()
    if review_count != candidate_count:
        return False
    stale_exists = ReviewScoreCandidate.objects.filter(generation=generation).filter(
        Q(review_updated__lt=F("review__updated"))
        | Q(review_updated__isnull=True, review__updated__isnull=False)
    )
    return not stale_exists.exists()


def _publish_candidate_scores(generation, chunk_size=500) -> None:
    """Bulk-copy every candidate value into ``Review.score``.

    Enumerates candidates at statement execution time (not a pre-locked id
    list), so reviews whose candidates landed in the gap between the first
    review snapshot and the generation lock are published too -- the caller
    has already re-verified completeness while holding the generation lock.
    """
    candidate_values = dict(
        ReviewScoreCandidate.objects.filter(generation=generation).values_list(
            "review_id", "value"
        )
    )
    review_ids = list(candidate_values)
    for offset in range(0, len(review_ids), chunk_size):
        chunk_ids = review_ids[offset : offset + chunk_size]
        reviews = list(Review.objects.filter(pk__in=chunk_ids))
        for review in reviews:
            review.score = candidate_values[review.pk]
        Review.objects.bulk_update(reviews, ["score"], batch_size=chunk_size)


def confirm_generation(generation) -> bool:
    """Atomically publish one complete pending generation.

    Fails closed for superseded/incomplete generations: no ``Review.score`` is
    touched in that case. On success, older generations lose their candidates
    (GC) while the generation rows stay as a persistent audit trail.
    """
    from pretalx.event.models import Event  # noqa: PLC0415 -- leaf import

    with transaction.atomic():
        Event.objects.select_for_update().get(pk=generation.event_id)
        # Stabilise the review set first: online writers serialise on their
        # review row, so edits to rows visible here block before touching any
        # generation lock and cannot interleave with publication.
        _lock_event_reviews(generation.event_id)
        # Lock every generation row in seq order with ONE statement before any
        # status UPDATE: online writers lock pending gens in the same seq order
        # and never lock confirmed gens, so no lock-inversion cycle is
        # possible. Holding all rows also makes the final status flips safe.
        all_generations = list(
            ScoreGeneration.objects.select_for_update()
            .filter(event_id=generation.event_id)
            .order_by("seq")
        )
        locked = next((g for g in all_generations if g.pk == generation.pk), None)
        if locked is None or locked.status != PENDING:
            return False
        # Fresh snapshot under the locks: the cursor worker and online writes
        # that finished in the gap are visible now. If anything is still
        # missing, fail closed -- the task re-enqueues instead of publishing.
        if not generation_is_complete(locked):
            return False
        if any(g.seq > locked.seq for g in all_generations):
            locked.status = SUPERSEDED
            locked.save(update_fields=["status"])
            return False

        _publish_candidate_scores(locked)
        locked.status = CONFIRMED
        locked.confirmed_at = now()
        locked.save(update_fields=["status", "confirmed_at"])
        ScoreGeneration.objects.filter(
            event_id=locked.event_id, status=CONFIRMED
        ).exclude(pk=locked.pk).update(status=SUPERSEDED)
        ReviewScoreCandidate.objects.filter(
            generation__event_id=locked.event_id
        ).exclude(generation=locked).delete()
    return True


def run_generation(generation_id, *, max_passes=3) -> str:
    """Drive one generation from pending to confirmation along its cursor.

    Returns ``"confirmed"``, ``"stale"`` (superseded), ``"missing"`` (unknown
    id) or ``"incomplete"`` (reviews kept changing; task should re-enqueue).
    """
    generation = (
        ScoreGeneration.objects.filter(pk=generation_id).select_related("event").first()
    )
    if generation is None:
        LOGGER.error("Could not find ScoreGeneration ID %s.", generation_id)
        return "missing"

    while True:
        outcome = process_generation_batch(generation)
        if outcome == "stale":
            return "stale"
        if outcome == "caught_up":
            break

    for _ in range(max_passes):
        if generation_is_complete(generation) and confirm_generation(generation):
            return "confirmed"
        if sweep_generation(generation) == 0:
            break

    status = (
        ScoreGeneration.objects.filter(pk=generation.pk)
        .values_list("status", flat=True)
        .first()
    )
    return "stale" if status != PENDING else "incomplete"
