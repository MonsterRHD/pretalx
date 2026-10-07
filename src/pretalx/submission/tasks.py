# SPDX-FileCopyrightText: 2017-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

import logging

from django_scopes import scope, scopes_disabled

from pretalx.celery_app import app
from pretalx.common.exceptions import SendMailException

LOGGER = logging.getLogger(__name__)


@app.task(
    name="pretalx.submission.apply_score_generation",
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def task_apply_score_generation(*, generation_id: int, attempt: int = 0):
    from pretalx.submission.domain.score_generation import (  # noqa: PLC0415 -- leaf
        run_generation,
    )
    from pretalx.submission.models import ScoreGeneration  # noqa: PLC0415 -- leaf

    max_attempts = 5
    with scopes_disabled():
        generation = (
            ScoreGeneration.objects.select_related("event")
            .filter(pk=generation_id)
            .first()
        )
    if not generation:
        LOGGER.error(
            "Could not find ScoreGeneration ID %s for review recalculation.",
            generation_id,
        )
        return

    with scope(event=generation.event):
        outcome = run_generation(generation_id)

    # Reviews kept changing while the worker ran: back off and retry a bounded
    # number of times. Every attempt is an idempotent resume from persisted
    # candidates, so giving up leaves a consistent state (confirmed reads are
    # unaffected) and a later settings commit re-enqueues the event.
    if outcome == "incomplete" and attempt < max_attempts:
        task_apply_score_generation.apply_async(
            kwargs={"generation_id": generation_id, "attempt": attempt + 1},
            countdown=60,
            ignore_result=True,
        )
    elif outcome == "incomplete":
        LOGGER.error(
            "ScoreGeneration %s still incomplete after %s attempts; "
            "staying on the previous confirmed generation.",
            generation_id,
            max_attempts,
        )
    return outcome


@app.task(name="pretalx.submission.export_question_files")
def task_export_question_files(*, question_id: int, cached_file_id: str):
    from pretalx.common.models.file import CachedFile  # noqa: PLC0415 -- leaf
    from pretalx.submission.domain.question import (  # noqa: PLC0415 -- leaf
        export_answer_files,
    )
    from pretalx.submission.models import Question  # noqa: PLC0415 -- leaf

    question = (
        Question.all_objects.with_scopes_disabled()
        .select_related("event")
        .filter(pk=question_id)
        .first()
    )
    cached_file = CachedFile.objects.filter(id=cached_file_id).first()

    if not question:
        LOGGER.error("Could not find Question ID %s for file export.", question_id)
        return None
    if not cached_file:
        LOGGER.error("Could not find CachedFile ID %s for file export.", cached_file_id)
        return None

    with scope(event=question.event):
        return export_answer_files(question=question, cached_file=cached_file)


@app.task(name="pretalx.submission.send_initial_mails")
def task_send_initial_mails(*, submission_id: int, person_id: int):
    from pretalx.person.models import User  # noqa: PLC0415 -- leaf
    from pretalx.submission.domain.submission import (  # noqa: PLC0415 -- leaf
        send_initial_mails,
    )
    from pretalx.submission.models import Submission  # noqa: PLC0415 -- leaf

    submission = (
        Submission.all_objects.with_scopes_disabled()
        .with_display_data()
        .filter(pk=submission_id)
        .first()
    )
    person = User.objects.filter(pk=person_id).first()

    if not submission:
        LOGGER.warning(
            "Could not find Submission ID %s for initial mails.", submission_id
        )
        return
    if not person:
        LOGGER.warning("Could not find User ID %s for initial mails.", person_id)
        return

    with scope(event=submission.event):
        try:
            send_initial_mails(submission, person=person)
        except SendMailException as exception:
            LOGGER.warning(str(exception))
