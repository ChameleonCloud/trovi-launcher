import uuid

from django.db import models


class Launch(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        READY = "ready"
        FAILED = "failed"
        CANCELLED = "cancelled"
        DELETED = "deleted"

    ACTIVE = [Status.PENDING, Status.RUNNING]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_sub = models.CharField(max_length=255, db_index=True)
    username = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)

    artifact_uuid = models.UUIDField(null=True, blank=True)
    version_slug = models.CharField(max_length=255, blank=True)

    site = models.CharField(max_length=64)
    project = models.CharField(max_length=255)
    arguments = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    step = models.CharField(max_length=32, blank=True)
    message = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)

    runner = models.CharField(max_length=32, blank=True)
    runner_ref = models.CharField(max_length=255, blank=True)
    callback_token_hash = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.id} ({self.status})"

    @property
    def is_active(self):
        return self.status in self.ACTIVE

    def resources(self):
        """Return [kind, id] for what this launch created and hasn't deleted."""
        resources = {}
        for event in self.events.all():
            for resource in event.data.get("created", {}).items():
                resources[resource] = True
            for resource in event.data.get("deleted", {}).items():
                resources.pop(resource, None)
        return [list(resource) for resource in resources]


class LaunchEvent(models.Model):
    class Level(models.TextChoices):
        INFO = "info"
        WARNING = "warning"
        ERROR = "error"

    launch = models.ForeignKey(Launch, models.CASCADE, related_name="events")
    created_at = models.DateTimeField(auto_now_add=True)
    step = models.CharField(max_length=32, blank=True)
    level = models.CharField(max_length=16, choices=Level.choices, default=Level.INFO)
    message = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
