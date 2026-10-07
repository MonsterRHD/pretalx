# SPDX-FileCopyrightText: 2018-present Tobias Kunze
# SPDX-License-Identifier: AGPL-3.0-only WITH LicenseRef-Pretalx-AGPL-3.0-Terms

from django.dispatch import receiver

from pretalx.common.signals import (
    minimum_interval,
    periodic_task,
    register_data_exporters,
)


@receiver(periodic_task)
@minimum_interval(minutes_after_success=1)
def recover_interrupted_schedule_releases(sender, **kwargs):
    """Resume or abort schedule release generations interrupted by a
    failing worker or process exit, so published marker, visible slots,
    cache and notifications can never drift apart."""
    from pretalx.schedule.tasks import (  # noqa: PLC0415 -- receiver
        task_recover_schedule_releases,
    )

    task_recover_schedule_releases.apply_async(ignore_result=True)


@receiver(register_data_exporters, dispatch_uid="exporter_builtin_ical")
def register_ical_exporter(sender, **kwargs):
    from pretalx.schedule.interfaces.exporters import (  # noqa: PLC0415 -- receiver
        ICalExporter,
    )

    return ICalExporter


@receiver(register_data_exporters, dispatch_uid="exporter_builtin_faved_ical")
def register_faved_ical_exporter(sender, **kwargs):
    from pretalx.schedule.interfaces.exporters import (  # noqa: PLC0415 -- receiver
        FavedICalExporter,
    )

    return FavedICalExporter


@receiver(register_data_exporters, dispatch_uid="exporter_builtin_xml")
def register_xml_exporter(sender, **kwargs):
    from pretalx.schedule.interfaces.exporters import (  # noqa: PLC0415 -- receiver
        FrabXmlExporter,
    )

    return FrabXmlExporter


@receiver(register_data_exporters, dispatch_uid="exporter_builtin_xcal")
def register_xcal_exporter(sender, **kwargs):
    from pretalx.schedule.interfaces.exporters import (  # noqa: PLC0415 -- receiver
        FrabXCalExporter,
    )

    return FrabXCalExporter


@receiver(register_data_exporters, dispatch_uid="exporter_builtin_json")
def register_json_exporter(sender, **kwargs):
    from pretalx.schedule.interfaces.exporters import (  # noqa: PLC0415 -- receiver
        FrabJsonExporter,
    )

    return FrabJsonExporter
