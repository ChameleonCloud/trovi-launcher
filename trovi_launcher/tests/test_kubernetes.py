from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from kubernetes import client, config

from trovi_launcher import runners
from trovi_launcher.models import Launch

IMAGE = "registry.test/trovi-provisioner"
SETTINGS = {"KUBERNETES": {"IMAGE": IMAGE, "NAMESPACE": "launches"}}


def job(deleting=False, **conditions):
    """A Job as read from the API, with conditions given as type=message."""
    return SimpleNamespace(
        metadata=SimpleNamespace(deletion_timestamp="2026-11-13" if deleting else None),
        status=SimpleNamespace(
            conditions=[
                SimpleNamespace(type=kind, status="True", message=message)
                for kind, message in conditions.items()
            ]
            or None
        ),
    )


@override_settings(TROVI_LAUNCHER=SETTINGS)
class KubernetesRunnerTests(SimpleTestCase):
    def setUp(self):
        self.api = mock.Mock()
        self.api.create_namespaced_job.return_value.metadata.uid = "job-uid"
        self.config = {}
        for target, value in [
            ("kubernetes.config.load_incluster_config", None),
            ("kubernetes.config.load_kube_config", None),
            ("kubernetes.client.BatchV1Api", self.api),
            ("kubernetes.client.CoreV1Api", self.api),
        ]:
            patcher = mock.patch(target, return_value=value)
            self.config[target.rsplit(".", 1)[1]] = patcher.start()
            self.addCleanup(patcher.stop)
        self.launch = Launch(runner="kubernetes", runner_ref="launches/job")

    def start(self):
        return runners.get("kubernetes").start(self.launch, {"OS_TOKEN": "token"})

    def test_start(self):
        ref = self.start()

        body = self.api.create_namespaced_job.call_args.args[1]
        name = body["metadata"]["name"]
        self.assertEqual(ref, f"launches/{name}")
        self.assertTrue(name.startswith(f"trovi-launch-{self.launch.id}-"))
        self.assertTrue(body["spec"]["suspend"])
        self.assertEqual(body["spec"]["backoffLimit"], 0)
        pod = body["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        container = pod["containers"][0]
        self.assertEqual(container["image"], IMAGE)
        self.assertEqual(
            container["command"],
            ["python", "-m", "trovi_launcher.provisioner.chameleon"],
        )
        self.assertEqual(container["envFrom"], [{"secretRef": {"name": name}}])
        secret = self.api.create_namespaced_secret.call_args.args[1]
        self.assertEqual(secret["metadata"]["name"], name)
        self.assertEqual(secret["stringData"], {"OS_TOKEN": "token"})
        self.assertEqual(secret["metadata"]["ownerReferences"][0]["uid"], "job-uid")
        # The Job only runs once its Secret exists
        self.assertEqual(
            [call[0] for call in self.api.method_calls],
            [
                "create_namespaced_job",
                "create_namespaced_secret",
                "patch_namespaced_job",
            ],
        )
        self.assertEqual(
            self.api.patch_namespaced_job.call_args.args[2],
            [{"op": "replace", "path": "/spec/suspend", "value": False}],
        )

    def test_each_run_gets_its_own_job(self):
        self.assertNotEqual(self.start(), self.start())

    @override_settings(
        TROVI_LAUNCHER=SETTINGS | {"PROVISIONER": ["python", "-m", "fake"]}
    )
    def test_provisioner_command(self):
        self.start()

        body = self.api.create_namespaced_job.call_args.args[1]
        container = body["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["python", "-m", "fake"])

    def test_failed_secret_deletes_job(self):
        self.api.create_namespaced_secret.side_effect = client.ApiException(status=403)

        with self.assertRaises(client.ApiException):
            self.start()

        name = self.api.create_namespaced_job.call_args.args[1]["metadata"]["name"]
        self.api.delete_namespaced_job.assert_called_once_with(
            name, "launches", propagation_policy="Foreground"
        )
        self.api.patch_namespaced_job.assert_not_called()

    def test_stop(self):
        runners.get("kubernetes").stop(self.launch)

        self.api.delete_namespaced_job.assert_called_once_with(
            "job", "launches", propagation_policy="Foreground"
        )

    def test_stop_after_job_is_gone(self):
        self.api.delete_namespaced_job.side_effect = client.ApiException(status=404)

        runners.get("kubernetes").stop(self.launch)

    def test_poll(self):
        deadline = "Job was active longer than specified deadline"
        for read, reason in [
            ({"return_value": job()}, None),
            ({"return_value": job(Complete="")}, runners.EXITED),
            (
                {"return_value": job(Failed=deadline)},
                f"Provisioner job failed: {deadline}",
            ),
            ({"return_value": job(deleting=True, Failed=deadline)}, None),
            ({"side_effect": client.ApiException(status=404)}, runners.EXITED),
        ]:
            with self.subTest(reason=reason, read=read):
                self.api.read_namespaced_job.configure_mock(**read)
                self.assertEqual(runners.get("kubernetes").poll(self.launch), reason)

    def test_poll_api_error(self):
        self.api.read_namespaced_job.side_effect = client.ApiException(status=403)

        with self.assertLogs("trovi_launcher.k8s", "WARNING"):
            self.assertIsNone(runners.get("kubernetes").poll(self.launch))

    @override_settings(TROVI_LAUNCHER={"KUBERNETES": {"IMAGE": IMAGE}})
    def test_namespace_defaults_to_the_hosts(self):
        with mock.patch(
            "trovi_launcher.k8s.open", mock.mock_open(read_data="portal\n"), create=True
        ):
            self.assertEqual(self.start().split("/")[0], "portal")
        with mock.patch(
            "trovi_launcher.k8s.open", side_effect=FileNotFoundError, create=True
        ):
            self.assertEqual(self.start().split("/")[0], "default")

    def test_kubeconfig_outside_a_cluster(self):
        self.config["load_incluster_config"].side_effect = config.ConfigException

        runners.get("kubernetes")

        self.config["load_kube_config"].assert_called_once()

    @override_settings(TROVI_LAUNCHER={"KUBERNETES": {}})
    def test_requires_image(self):
        with self.assertRaises(ImproperlyConfigured):
            runners.get("kubernetes")
