import logging
from secrets import token_hex

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from kubernetes import client, config

from .runners import EXITED

LOG = logging.getLogger(__name__)

# The provisioner in the image that the Dockerfile builds
DEFAULT_PROVISIONER = ["python", "-m", "trovi_launcher.provisioner.chameleon"]
NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
# Longer than the provisioner's lease, server and SSH timeouts together
DEADLINE = 2 * 60 * 60
# Long enough for a cancelled provisioner to delete its lease and server
GRACE_PERIOD = 120


class KubernetesRunner:
    """Runs the provisioner as a Kubernetes Job, with its environment in a Secret.

    The Job owns the Secret, so the Secret is deleted with it. The Job is
    created suspended, and resumed once the Secret exists.
    """

    def __init__(self):
        self.options = settings.TROVI_LAUNCHER.get("KUBERNETES", {})
        if not self.options.get("IMAGE"):
            raise ImproperlyConfigured("Set TROVI_LAUNCHER['KUBERNETES']['IMAGE']")
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.batch, self.core = client.BatchV1Api(), client.CoreV1Api()

    def start(self, launch, env):
        namespace = self.namespace()
        # Cleaning up runs the launch again, so each run needs its own name
        name = f"trovi-launch-{launch.id}-{token_hex(3)}"
        labels = {
            "app.kubernetes.io/name": "trovi-launcher",
            "trovi-launcher/launch": str(launch.id),
        }
        job = self.batch.create_namespaced_job(namespace, self.job(name, labels))
        owner = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": name,
            "uid": job.metadata.uid,
        }
        try:
            self.core.create_namespaced_secret(
                namespace,
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": {
                        "name": name,
                        "labels": labels,
                        "ownerReferences": [owner],
                    },
                    "stringData": env,
                },
            )
            self.batch.patch_namespaced_job(
                name,
                namespace,
                [{"op": "replace", "path": "/spec/suspend", "value": False}],
            )
        except Exception:
            self.delete(namespace, name)
            raise
        return f"{namespace}/{name}"

    def stop(self, launch):
        self.delete(*launch.runner_ref.split("/"))

    def poll(self, launch):
        namespace, name = launch.runner_ref.split("/")
        try:
            job = self.batch.read_namespaced_job(name, namespace)
        except client.ApiException as e:
            if e.status == 404:
                return EXITED
            # Launches are reconciled as pages load, which an API error shouldn't
            # break. The launch's deadline still applies.
            LOG.warning("Could not read job %s: %s", launch.runner_ref, e)
            return None
        # A Job being deleted has been cancelled, and its provisioner is
        # deleting what it created
        if job.metadata.deletion_timestamp:
            return None
        conditions = {
            c.type: c for c in job.status.conditions or [] if c.status == "True"
        }
        if failed := conditions.get("Failed"):
            return f"Provisioner job failed: {failed.message}"
        return EXITED if "Complete" in conditions else None

    def namespace(self):
        """Return the NAMESPACE setting, or else the host's own namespace."""
        if namespace := self.options.get("NAMESPACE"):
            return namespace
        try:
            with open(NAMESPACE_FILE) as f:
                return f.read().strip()
        except FileNotFoundError:
            return "default"

    def job(self, name, labels):
        container = {
            "name": "provisioner",
            "image": self.options["IMAGE"],
            "command": settings.TROVI_LAUNCHER.get("PROVISIONER")
            or DEFAULT_PROVISIONER,
            "envFrom": [{"secretRef": {"name": name}}],
            "resources": self.options.get("RESOURCES", {}),
        }
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "suspend": True,
                # A retry would provision a second node
                "backoffLimit": 0,
                "activeDeadlineSeconds": DEADLINE,
                "ttlSecondsAfterFinished": self.options.get("TTL", 24 * 60 * 60),
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "terminationGracePeriodSeconds": GRACE_PERIOD,
                        "containers": [container],
                    },
                },
            },
        }

    def delete(self, namespace, name):
        """Delete a Job, which stays until its pod has exited."""
        try:
            self.batch.delete_namespaced_job(
                name, namespace, propagation_policy="Foreground"
            )
        except client.ApiException as e:
            if e.status != 404:
                raise
