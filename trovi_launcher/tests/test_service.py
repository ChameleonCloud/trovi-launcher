import json
from datetime import timedelta
from unittest import mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from trovi_launcher import service
from trovi_launcher.models import Launch

FIELDS = {
    "site": "CHI@UC",
    "project": "CHI-000000",
    "arguments": {"node_type": "compute_skylake", "lease_duration_hours": 2},
    "user_sub": "sub",
}
CALLBACK = "http://launcher.test"


class ServiceTests(TestCase):
    def setUp(self):
        self.runner = mock.Mock()
        self.runner.start.return_value = "1234"
        self.runner.poll.return_value = None
        for target, value in [
            ("trovi_launcher.runners.get", self.runner),
            ("trovi_launcher.credentials.resolve", {"OS_TOKEN": "keystone-token"}),
        ]:
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def create(self, **overrides):
        return service.create_launch(
            subject_token="token", callback_base_url=CALLBACK, **FIELDS | overrides
        )

    def env(self):
        return self.runner.start.call_args.args[1]

    def test_create_starts_runner(self):
        launch = self.create()

        self.assertEqual(launch.runner_ref, "1234")
        path = reverse("trovi_launcher:events", args=[launch.id])
        self.assertEqual(self.env()["CALLBACK_URL"], f"{CALLBACK}{path}")
        self.assertEqual(self.env()["OS_TOKEN"], "keystone-token")
        self.assertEqual(
            json.loads(self.env()["LAUNCH_PARAMS"]),
            {
                "id": str(launch.id),
                "site": "CHI@UC",
                "project": "CHI-000000",
                "node_type": "compute_skylake",
                "lease_duration_hours": 2,
            },
        )

    def test_arguments_cannot_replace_launch_fields(self):
        self.create(arguments={"id": "other", "project": "other", "cleanup": "x"})

        params = json.loads(self.env()["LAUNCH_PARAMS"])
        self.assertNotEqual(params["id"], "other")
        self.assertEqual(params["project"], "CHI-000000")
        self.assertNotIn("LAUNCH_CLEANUP", self.env())

    def test_credential_failure_creates_nothing(self):
        with mock.patch(
            "trovi_launcher.credentials.resolve", side_effect=PermissionDenied
        ):
            with self.assertRaises(PermissionDenied):
                self.create()

        self.assertFalse(Launch.objects.exists())
        self.runner.start.assert_not_called()

    def test_create_requires_site_and_project(self):
        for field in ["site", "project"]:
            with self.subTest(field), self.assertRaises(ValidationError):
                self.create(**{field: ""})
        self.create(arguments={})

    def test_start_failure_fails_launch(self):
        self.runner.start.side_effect = OSError("boom")
        with self.assertRaises(OSError):
            self.create()

        launch = Launch.objects.get()
        self.assertEqual(launch.status, Launch.Status.FAILED)
        self.assertEqual(launch.message, "Runner failed to start: boom")

    def test_events_update_launch(self):
        launch = self.create()
        token = self.env()["CALLBACK_TOKEN"]
        service.record_event(launch, token, {"step": "lease", "message": "Reserving"})
        service.record_event(
            launch,
            token,
            {
                "step": "finalize",
                "status": "ready",
                "result": {
                    "floating_ip": "192.0.2.10",
                    "lease_end": "2026-11-06T12:00:00+00:00",
                },
            },
        )

        launch.refresh_from_db()
        self.assertEqual(launch.status, Launch.Status.READY)
        self.assertEqual(launch.step, "finalize")
        self.assertEqual(launch.message, "Reserving")
        self.assertEqual(launch.result["floating_ip"], "192.0.2.10")
        self.assertIsNotNone(launch.finished_at)
        self.assertEqual(launch.events.count(), 2)
        with self.assertRaises(PermissionDenied):
            service.record_event(launch, token, {"step": "late"})

    def test_event_rejects_bad_token_and_status(self):
        launch = self.create()
        with self.assertRaises(PermissionDenied):
            service.record_event(launch, "wrong", {})
        with self.assertRaises(ValidationError):
            service.record_event(
                launch, self.env()["CALLBACK_TOKEN"], {"status": "pending"}
            )
        self.assertEqual(launch.events.count(), 0)

    def test_clean_up_deletes_remaining_resources(self):
        launch = self.create()
        token = self.env()["CALLBACK_TOKEN"]
        for event in [
            {"data": {"created": ["lease", "l"]}},
            {"data": {"created": ["keypair", "k"]}},
            {"data": {"created": ["server", "s"]}},
            {"data": {"deleted": ["keypair", "k"]}},
            {"status": "ready"},
        ]:
            service.record_event(launch, token, event)
        service.clean_up_launch(
            launch, subject_token="token", callback_base_url=CALLBACK
        )

        self.assertEqual(launch.status, Launch.Status.RUNNING)
        cleanup = json.loads(self.env()["LAUNCH_CLEANUP"])
        self.assertEqual(cleanup, [["lease", "l"], ["server", "s"]])
        service.record_event(
            launch,
            self.env()["CALLBACK_TOKEN"],
            {"status": "deleted", "data": {"deleted": ["server", "s"]}},
        )
        self.assertEqual(launch.resources(), [["lease", "l"]])

    def test_clean_up_skips_active_and_empty_launches(self):
        launch = self.create()
        token = self.env()["CALLBACK_TOKEN"]
        service.record_event(launch, token, {"data": {"created": ["lease", "l"]}})
        service.clean_up_launch(
            launch, subject_token="token", callback_base_url=CALLBACK
        )
        service.record_event(
            launch, token, {"status": "ready", "data": {"deleted": ["lease", "l"]}}
        )
        service.clean_up_launch(
            launch, subject_token="token", callback_base_url=CALLBACK
        )

        self.runner.start.assert_called_once()

    def test_reconcile_fails_exited_launch(self):
        launch = self.create()
        self.runner.poll.return_value = "Provisioner exited"
        service.reconcile(launch)

        self.assertEqual(launch.status, Launch.Status.FAILED)
        self.assertEqual(launch.events.get().message, "Provisioner exited")

    def test_reconcile_stops_launch_past_deadline(self):
        launch = self.create()
        Launch.objects.filter(pk=launch.pk).update(
            updated_at=timezone.now() - timedelta(hours=2)
        )
        launch.refresh_from_db()
        service.reconcile(launch)

        self.runner.stop.assert_called_once()
        self.assertEqual(launch.status, Launch.Status.FAILED)

    def test_launch_stopped_at_deadline_reports_its_clean_up(self):
        launch = self.create()
        token = self.env()["CALLBACK_TOKEN"]
        service.record_event(launch, token, {"data": {"created": ["lease", "l"]}})
        Launch.objects.filter(pk=launch.pk).update(
            updated_at=timezone.now() - timedelta(hours=2)
        )
        launch.refresh_from_db()
        service.reconcile(launch)
        service.record_event(launch, token, {"data": {"deleted": ["lease", "l"]}})
        service.record_event(launch, token, {"status": "cancelled"})

        self.assertEqual(launch.status, Launch.Status.CANCELLED)
        self.assertEqual(launch.resources(), [])

    def test_reconcile_leaves_running_launch(self):
        launch = self.create()
        service.reconcile(launch)

        self.assertEqual(launch.status, Launch.Status.PENDING)

    def test_reconcile_keeps_result_reported_meanwhile(self):
        launch = self.create()
        stale = Launch.objects.get(pk=launch.pk)
        service.record_event(launch, self.env()["CALLBACK_TOKEN"], {"status": "ready"})
        self.runner.poll.return_value = "Provisioner exited"
        service.reconcile(stale)

        self.assertEqual(stale.status, Launch.Status.READY)
