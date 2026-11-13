import os
import signal
import sys
import time
from unittest import mock

from django.test import LiveServerTestCase

from trovi_launcher import service
from trovi_launcher.models import Launch

from .test_service import FIELDS

FAKE_PROVISIONER = "trovi_launcher.tests.fake_provisioner"


class LocalRunnerTests(LiveServerTestCase):
    def launch(self, **env):
        settings = self.settings(
            TROVI_LAUNCHER={"PROVISIONER": [sys.executable, "-m", FAKE_PROVISIONER]}
        )
        environ = mock.patch.dict(os.environ, {"FAKE_PROVISIONER_DELAY": "0"} | env)
        credentials = mock.patch("trovi_launcher.credentials.resolve", return_value={})
        with settings, environ, credentials:
            return service.create_launch(
                subject_token="token",
                callback_base_url=self.live_server_url,
                **FIELDS,
            )

    def wait_for(self, launch, status):
        for _ in range(100):
            launch.refresh_from_db()
            if launch.status == status:
                return
            time.sleep(0.1)
        self.fail(f"Launch is {launch.status}, expected {status}")

    def test_ready(self):
        launch = self.launch()
        self.wait_for(launch, Launch.Status.READY)

        self.assertEqual(launch.result["floating_ip"], "192.0.2.10")
        self.assertEqual(launch.message, "ssh cc@192.0.2.10")

    def test_failure(self):
        launch = self.launch(FAKE_PROVISIONER_FAIL_AT="server")
        self.wait_for(launch, Launch.Status.FAILED)

        self.assertEqual(launch.step, "server")

    def test_cancel(self):
        launch = self.launch(FAKE_PROVISIONER_DELAY="0.5")
        self.wait_for(launch, Launch.Status.RUNNING)
        service.cancel_launch(launch)
        self.wait_for(launch, Launch.Status.CANCELLED)

    def test_reconcile_after_crash(self):
        launch = self.launch(FAKE_PROVISIONER_DELAY="5")
        self.wait_for(launch, Launch.Status.RUNNING)
        pid = int(launch.runner_ref)
        os.killpg(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        service.reconcile(launch)

        self.assertEqual(launch.status, Launch.Status.FAILED)
