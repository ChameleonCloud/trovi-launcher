import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from urllib.parse import urljoin

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import credentials, runners
from .models import Launch, LaunchEvent

DEADLINE = timedelta(hours=1)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_launch(subject_token, **fields):
    """Start a launch from Launch fields: site, project, arguments and who asked."""
    launch = Launch(**fields, runner=settings.TROVI_LAUNCHER.get("RUNNER", "local"))
    launch.full_clean()
    _start(launch, credentials.resolve(launch, subject_token))
    return launch


def clean_up_launch(launch, subject_token):
    """Delete what a finished launch created, such as its server and lease."""
    resources = launch.resources()
    if launch.is_active or not resources:
        return
    env = credentials.resolve(launch, subject_token)
    launch.status, launch.step = Launch.Status.RUNNING, "cleanup"
    launch.finished_at = None
    _start(launch, env, cleanup=resources)


def _start(launch, env, cleanup=None):
    runner = runners.get(launch.runner)
    token = secrets.token_urlsafe(32)
    launch.callback_token_hash = _hash(token)
    launch.save()
    try:
        launch.runner_ref = runner.start(
            launch, env | _provisioner_env(launch, token, cleanup)
        )
    except Exception as e:
        _finish(launch, Launch.Status.FAILED, f"Runner failed to start: {e}")
        raise
    launch.save(update_fields=["runner_ref", "updated_at"])


def _provisioner_env(launch, token, cleanup):
    path = reverse("trovi_launcher:events", args=[launch.id])
    params = launch.arguments | {
        "id": str(launch.id),
        "site": launch.site,
        "project": launch.project,
    }
    env = {
        "CALLBACK_URL": urljoin(settings.TROVI_LAUNCHER["CALLBACK_BASE_URL"], path),
        "CALLBACK_TOKEN": token,
        "LAUNCH_PARAMS": json.dumps(params),
    }
    if cleanup:
        env["LAUNCH_CLEANUP"] = json.dumps(cleanup)
    return env


def cancel_launch(launch):
    if launch.is_active and launch.runner_ref:
        runners.get(launch.runner).stop(launch)


@transaction.atomic
def record_event(launch, token, event):
    if not launch.callback_token_hash or not hmac.compare_digest(
        launch.callback_token_hash, _hash(token)
    ):
        raise PermissionDenied("Invalid callback token")
    status = event.get("status", Launch.Status.RUNNING)
    if status == Launch.Status.PENDING or status not in Launch.Status.values:
        raise ValidationError(f"Invalid status {status!r}")

    entry = LaunchEvent(
        launch=launch,
        step=event.get("step", ""),
        level=event.get("level", LaunchEvent.Level.INFO),
        message=event.get("message", ""),
        data=event.get("data", {}),
    )
    entry.full_clean()
    entry.save()

    launch.status = status
    launch.step = entry.step or launch.step
    launch.message = entry.message or launch.message
    if "result" in event:
        launch.result = event["result"]
    if not launch.is_active:
        launch.finished_at = timezone.now()
        launch.callback_token_hash = ""
    launch.save(
        update_fields=[
            "status",
            "step",
            "message",
            "result",
            "finished_at",
            "callback_token_hash",
            "updated_at",
        ]
    )


def reconcile(launch):
    if not launch.is_active:
        return
    reason = launch.runner_ref and runners.get(launch.runner).poll(launch)
    if not reason:
        if timezone.now() < launch.updated_at + DEADLINE:
            return
        cancel_launch(launch)
        reason = "Launch made no progress before its deadline"
    _finish(launch, Launch.Status.FAILED, reason)


def _finish(launch, status, message):
    now = timezone.now()
    with transaction.atomic():
        updated = Launch.objects.filter(pk=launch.pk, status__in=Launch.ACTIVE).update(
            status=status,
            message=message,
            finished_at=now,
            updated_at=now,
        )
        if updated:
            launch.events.create(level=LaunchEvent.Level.ERROR, message=message)
    launch.refresh_from_db()
