from unittest import mock

from django.test import Client, TestCase
from django.urls import reverse

from trovi_launcher import service

from .test_service import FIELDS


class EventViewTests(TestCase):
    def setUp(self):
        runner = mock.Mock()
        runner.start.return_value = "1234"
        with (
            mock.patch("trovi_launcher.runners.get", return_value=runner),
            mock.patch("trovi_launcher.credentials.resolve", return_value={}),
        ):
            self.launch = service.create_launch(subject_token="token", **FIELDS)
        self.token = runner.start.call_args.args[1]["CALLBACK_TOKEN"]
        self.url = reverse("trovi_launcher:events", args=[self.launch.id])
        self.client = Client(enforce_csrf_checks=True)

    def post(self, body, token=None):
        return self.client.post(
            self.url,
            body,
            content_type="application/json",
            headers={"Authorization": f"Bearer {token or self.token}"},
        )

    def test_records_event(self):
        response = self.post({"step": "lease", "message": "Reserving"})

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.launch.events.get().message, "Reserving")

    def test_rejects_bad_token(self):
        self.assertEqual(self.post({}, token="wrong").status_code, 403)

    def test_rejects_bad_body(self):
        self.assertEqual(self.post("not json").status_code, 400)
        self.assertEqual(self.post({"status": "bogus"}).status_code, 400)
