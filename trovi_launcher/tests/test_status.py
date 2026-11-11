import json
import os
from unittest import TestCase, mock

from trovi_launcher.provisioner import status


def failing(error):
    def provision(launch):
        launch.enter("lease", "Reserving")
        launch.track("lease", "l")
        launch.enter("server", "Launching")
        launch.track("server", "s")
        raise error

    return provision


class RunTests(TestCase):
    def setUp(self):
        self.events = []
        self.deleted = []
        for patcher in [
            mock.patch.object(status, "post_event", self.events.append),
            mock.patch.object(status.signal, "signal"),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_status(self, provision, fail_delete=(), launch_cleanup=None, **params):
        def delete(kind, resource_id):
            if kind in fail_delete:
                raise RuntimeError("Conflict")
            self.deleted.append(kind)

        environ = {"LAUNCH_PARAMS": json.dumps({"id": "abc"} | params)}
        if launch_cleanup:
            environ["LAUNCH_CLEANUP"] = json.dumps(launch_cleanup)
        with mock.patch.dict(os.environ, environ):
            return status.run(provision, delete)

    def ledger(self, action):
        return [
            next(iter(e["data"][action]))
            for e in self.events
            if action in e.get("data", {})
        ]

    def test_ready(self):
        def provision(launch):
            launch.enter("finalize", "Finishing")
            return {"floating_ip": "192.0.2.10"}

        self.assertEqual(self.run_status(provision), 0)

        self.assertEqual(
            self.events[-1],
            {
                "step": "finalize",
                "status": "ready",
                "message": "ssh cc@192.0.2.10",
                "result": {"floating_ip": "192.0.2.10"},
            },
        )

    def test_argument_named_cleanup_still_provisions(self):
        provision = mock.Mock(return_value={"floating_ip": "192.0.2.10"})
        self.assertEqual(self.run_status(provision, cleanup="x"), 0)

        provision.assert_called_once()

    def test_failure_deletes_created_resources_newest_first(self):
        code = self.run_status(failing(RuntimeError("No valid host")))

        self.assertEqual(code, 1)
        self.assertEqual(self.deleted, ["server", "lease"])
        self.assertEqual(self.ledger("deleted"), ["server", "lease"])
        self.assertEqual(
            self.events[-1],
            {
                "step": "server",
                "status": "failed",
                "level": "error",
                "message": "RuntimeError: No valid host",
            },
        )

    def test_cancel_deletes_created_resources(self):
        self.assertEqual(self.run_status(failing(status.Cancelled())), 1)

        self.assertEqual(self.deleted, ["server", "lease"])
        self.assertEqual(self.events[-1]["status"], "cancelled")

    def test_failed_delete_is_a_warning(self):
        self.run_status(failing(RuntimeError()), fail_delete=["server"])

        self.assertEqual(self.deleted, ["lease"])
        self.assertIn(
            {
                "step": "server",
                "level": "warning",
                "message": "Could not delete server s: Conflict",
            },
            self.events,
        )

    def test_cleanup_deletes_newest_first(self):
        provision = mock.Mock()
        cleanup = [["lease", "l"], ["server", "s"]]
        self.assertEqual(self.run_status(provision, launch_cleanup=cleanup), 0)

        provision.assert_not_called()
        self.assertEqual(self.deleted, ["server", "lease"])
        self.assertEqual(self.events[-1]["status"], "deleted")

    def test_cleanup_fails_when_a_resource_remains(self):
        cleanup = [["lease", "l"], ["server", "s"]]
        code = self.run_status(
            mock.Mock(), fail_delete=["server"], launch_cleanup=cleanup
        )

        self.assertEqual(code, 1)
        self.assertEqual(self.ledger("deleted"), ["lease"])
        self.assertEqual(self.events[-1]["status"], "failed")
