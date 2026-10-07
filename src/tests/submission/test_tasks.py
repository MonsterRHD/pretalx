# SPDX-FileCopyrightText: 2026-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core import mail as djmail
from django_scopes import scope

from pretalx.common.exceptions import SendMailException
from pretalx.submission.domain.score_generation import (
    commit_score_generation,
    ensure_baseline_generation,
)
from pretalx.submission.models import ScoreGenerationStatus
from pretalx.submission.tasks import (
    task_apply_score_generation,
    task_export_question_files,
    task_send_initial_mails,
)
from tests.factories import (
    CachedFileFactory,
    EventFactory,
    QuestionFactory,
    ReviewFactory,
    ReviewScoreCategoryFactory,
    ReviewScoreFactory,
    SpeakerFactory,
    SubmissionFactory,
    UserFactory,
)

pytestmark = [pytest.mark.unit, pytest.mark.django_db]


def test_task_apply_score_generation_confirms_pending_generation():
    event = EventFactory()
    event.score_categories.all().delete()
    category = ReviewScoreCategoryFactory(
        event=event, weight=Decimal("1.0"), is_independent=False
    )
    option = ReviewScoreFactory(category=category, value=Decimal("3.0"))
    submission = SubmissionFactory(event=event)
    review = ReviewFactory(submission=submission, score=Decimal("3.0"))
    review.scores.add(option)

    with scope(event=event):
        ensure_baseline_generation(event)
        category.weight = Decimal("2.0")
        category.save()
        generation = commit_score_generation(event)

    result = task_apply_score_generation(generation_id=generation.pk)

    assert result == "confirmed"
    generation.refresh_from_db()
    assert generation.status == ScoreGenerationStatus.CONFIRMED
    review.refresh_from_db()
    assert review.score == Decimal("6.0")


def test_task_apply_score_generation_missing_generation():
    assert task_apply_score_generation(generation_id=99999) is None


def test_task_apply_score_generation_is_redelivery_safe():
    assert task_apply_score_generation.acks_late is True
    assert task_apply_score_generation.reject_on_worker_lost is True


def test_task_apply_score_generation_re_enqueues_when_incomplete():
    event = EventFactory()
    with scope(event=event):
        generation = ensure_baseline_generation(event)

    with patch(
        "pretalx.submission.domain.score_generation.run_generation",
        return_value="incomplete",
    ), patch.object(task_apply_score_generation, "apply_async") as apply_async:
        result = task_apply_score_generation(generation_id=generation.pk)

    assert result == "incomplete"
    apply_async.assert_called_once()
    assert apply_async.call_args.kwargs["kwargs"] == {
        "generation_id": generation.pk,
        "attempt": 1,
    }


def test_task_apply_score_generation_stops_re_enqueue_after_max_attempts():
    event = EventFactory()
    with scope(event=event):
        generation = ensure_baseline_generation(event)

    with patch(
        "pretalx.submission.domain.score_generation.run_generation",
        return_value="incomplete",
    ), patch.object(task_apply_score_generation, "apply_async") as apply_async:
        result = task_apply_score_generation(
            generation_id=generation.pk, attempt=5
        )

    assert result == "incomplete"
    apply_async.assert_not_called()


def test_task_export_question_files_missing_question():
    cached_file = CachedFileFactory()
    result = task_export_question_files(
        question_id=99999, cached_file_id=str(cached_file.id)
    )
    assert result is None
    cached_file.refresh_from_db()
    assert not cached_file.file


def test_task_export_question_files_missing_cached_file():
    question = QuestionFactory()
    result = task_export_question_files(
        question_id=question.pk, cached_file_id="00000000-0000-0000-0000-000000000000"
    )
    assert result is None


def test_task_export_question_files_delegates():
    question = QuestionFactory(variant="file")
    cached_file = CachedFileFactory()

    with (
        patch(
            "pretalx.submission.domain.question.export_answer_files",
            return_value=str(cached_file.id),
        ) as delegate,
        scope(),
    ):
        result = task_export_question_files(
            question_id=question.pk, cached_file_id=str(cached_file.id)
        )

    assert result == str(cached_file.id)
    delegate.assert_called_once_with(question=question, cached_file=cached_file)


def test_task_send_initial_mails_delegates():
    event = EventFactory()
    submission = SubmissionFactory(event=event)
    user = UserFactory()
    speaker = SpeakerFactory(event=event, user=user)
    submission.speakers.add(speaker)

    djmail.outbox = []

    with scope():
        task_send_initial_mails(submission_id=submission.pk, person_id=user.pk)

    assert len(djmail.outbox) == 1
    assert djmail.outbox[0].to == [user.email]


def test_task_send_initial_mails_missing_submission():
    user = UserFactory()
    djmail.outbox = []

    task_send_initial_mails(submission_id=99999, person_id=user.pk)

    assert len(djmail.outbox) == 0


def test_task_send_initial_mails_missing_user():
    submission = SubmissionFactory()
    djmail.outbox = []

    task_send_initial_mails(submission_id=submission.pk, person_id=99999)

    assert len(djmail.outbox) == 0


def test_task_send_initial_mails_handles_send_mail_exception():
    event = EventFactory()
    submission = SubmissionFactory(event=event)
    user = UserFactory()
    speaker = SpeakerFactory(event=event, user=user)
    submission.speakers.add(speaker)

    djmail.outbox = []
    with patch(
        "pretalx.submission.domain.submission.send_initial_mails",
        side_effect=SendMailException("SMTP error"),
    ):
        task_send_initial_mails(submission_id=submission.pk, person_id=user.pk)

    assert len(djmail.outbox) == 0
