# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
"""All read models must show the confirmed generation while a newer one is
pending, then switch atomically after confirmation."""

from decimal import Decimal

import pytest
from django.db.models import F
from django_scopes import scope

from pretalx.orga.forms.export import ReviewExportForm, ScheduleExportForm
from pretalx.submission.domain import score_generation as sg
from pretalx.submission.domain.queries.review import annotate_aggregate_scores
from tests.factories import (
    EventFactory,
    ReviewFactory,
    ReviewScoreCategoryFactory,
    ReviewScoreFactory,
    SubmissionFactory,
)
from tests.utils import make_orga_user

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def _two_generation_setup():
    """G1: categories A(value 2) and B(value 5) both active. G2: B inactive.

    Therefore sub_x (score in B) ranks first under G1 and drops out under G2,
    while sub_y (score in A) stays at 2 under both.
    """
    event = EventFactory()
    event.score_categories.all().delete()
    category_a = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("1.0"), active=True, is_independent=False
    )
    category_b = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("1.0"), active=True, is_independent=False
    )
    score_a = ReviewScoreFactory(category=category_a, value=Decimal("2.0"))
    score_b = ReviewScoreFactory(category=category_b, value=Decimal("5.0"))
    sub_x = SubmissionFactory(event=event)
    sub_y = SubmissionFactory(event=event)
    review_x = ReviewFactory(submission=sub_x, score=None)
    review_y = ReviewFactory(submission=sub_y, score=None)
    review_x.scores.add(score_b)
    review_y.scores.add(score_a)

    with scope(event=event):
        sg.ensure_baseline_generation(event)
        sg.refresh_review_scores(review_x)
        sg.refresh_review_scores(review_y)
        category_b.active = False
        category_b.save()
        g2 = sg.commit_score_generation(event)
        # Half-finished window: only sub_x's G2 candidate exists (B inactive
        # means None), sub_y has not been processed by the worker yet.
        sg.refresh_review_scores(review_x)

    return event, sub_x, sub_y, review_x, review_y, g2


def _aggregates(event, submissions):
    rows = annotate_aggregate_scores(
        event.submissions.filter(pk__in=[s.pk for s in submissions])
    ).order_by(F("median_score").desc(nulls_last=True))
    return [(row.code, row.median_score, row.mean_score) for row in rows]


def test_aggregate_annotations_read_confirmed_generation_during_window():
    event, sub_x, sub_y, review_x, review_y, g2 = _two_generation_setup()

    with scope(event=event):
        aggregates = _aggregates(event, [sub_x, sub_y])

    # G1 values (x=5, y=2), x ranks first despite the half-written G2 candidate.
    assert aggregates == [
        (sub_x.code, pytest.approx(5.0), pytest.approx(5.0)),
        (sub_y.code, pytest.approx(2.0), pytest.approx(2.0)),
    ]

    with scope(event=event):
        assert sg.run_generation(g2.id) == "confirmed"
        aggregates = _aggregates(event, [sub_x, sub_y])

    # G2 values: x dropped out (None ranks last), y stays at 2.
    assert aggregates == [
        (sub_y.code, pytest.approx(2.0), pytest.approx(2.0)),
        (sub_x.code, None, None),
    ]


def test_submission_model_properties_read_confirmed_generation():
    event, sub_x, sub_y, review_x, review_y, g2 = _two_generation_setup()

    with scope(event=event):
        assert sub_x.median_score == pytest.approx(5.0)
        assert sub_x.mean_score == pytest.approx(5.0)
        assert sub_y.mean_score == pytest.approx(2.0)

        sg.run_generation(g2.id)
        x = type(sub_x).objects.get(pk=sub_x.pk)
        y = type(sub_y).objects.get(pk=sub_y.pk)
        assert x.median_score is None
        assert x.mean_score is None
        assert y.mean_score == pytest.approx(2.0)


def test_review_export_reads_confirmed_generation_during_window():
    event, sub_x, sub_y, review_x, review_y, g2 = _two_generation_setup()
    user = make_orga_user(event)
    label = "Score"

    def exported_scores():
        form = ReviewExportForm(
            event=event, user=user, data={"export_format": "json", "target": "all"}
        )
        assert form.is_valid(), form.errors
        with scope(event=event):
            queryset = form.get_queryset().order_by("pk")
            data = form.get_data(queryset, ["score"], [])
        # Reviews are created in (sub_x, sub_y) order; preserve that mapping.
        return {
            submission_code: row[label]
            for submission_code, row in zip([sub_x.code, sub_y.code], data, strict=True)
        }

    scores = exported_scores()
    assert scores[sub_x.code] == Decimal("5.00")
    assert scores[sub_y.code] == Decimal("2.00")

    with scope(event=event):
        sg.run_generation(g2.id)

    scores = exported_scores()
    assert scores[sub_x.code] is None
    assert scores[sub_y.code] == Decimal("2.00")


def test_consumers_stay_on_g1_through_unfinished_g2_and_switch_to_g3():
    event, sub_x, sub_y, review_x, review_y, g2 = _two_generation_setup()

    with scope(event=event):
        # G2 never finishes; a further settings commit supersedes it.
        g3 = sg.commit_score_generation(event)
        assert sg.run_generation(g2.id) == "stale"
        # Reads never left G1 during the G2/G3 churn.
        assert type(sub_x).objects.get(pk=sub_x.pk).mean_score == pytest.approx(5.0)

        assert sg.run_generation(g3.id) == "confirmed"
        # G3 froze the same rules as G2 (B inactive): x drops out.
        assert type(sub_x).objects.get(pk=sub_x.pk).mean_score is None
        assert type(sub_y).objects.get(pk=sub_y.pk).mean_score == pytest.approx(2.0)


def test_sessions_export_median_attribute_reads_confirmed_generation():
    event, sub_x, sub_y, review_x, review_y, g2 = _two_generation_setup()
    user = make_orga_user(event)

    def median_values():
        form = ScheduleExportForm(event=event, user=user)
        with scope(event=event):
            return {
                submission.code: form.get_object_attribute(submission, "median_score")
                for submission in [
                    type(sub_x).objects.get(pk=sub_x.pk),
                    type(sub_y).objects.get(pk=sub_y.pk),
                ]
            }

    values = median_values()
    assert values[sub_x.code] == pytest.approx(5.0)
    assert values[sub_y.code] == pytest.approx(2.0)

    with scope(event=event):
        sg.run_generation(g2.id)

    values = median_values()
    assert values[sub_x.code] is None
    assert values[sub_y.code] == pytest.approx(2.0)
