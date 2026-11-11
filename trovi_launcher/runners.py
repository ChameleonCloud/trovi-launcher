import os
import signal
import subprocess
import sys

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

EXITED = "Provisioner exited without reporting a result"
# Any command that follows the contract in trovi_launcher/provisioner/status.py
DEFAULT_PROVISIONER = [sys.executable, "-m", "trovi_launcher.provisioner.chameleon"]


class LocalRunner:
    """Runs the provisioner as a local process. For development only."""

    def start(self, launch, env):
        process = subprocess.Popen(
            settings.TROVI_LAUNCHER.get("PROVISIONER") or DEFAULT_PROVISIONER,
            env=os.environ | env,
            start_new_session=True,
        )
        return str(process.pid)

    def stop(self, launch):
        try:
            os.killpg(int(launch.runner_ref), signal.SIGTERM)
        except ProcessLookupError:
            pass

    def poll(self, launch):
        pid = int(launch.runner_ref)
        try:
            exited = os.waitpid(pid, os.WNOHANG)[0] != 0
        except ChildProcessError:
            exited = not _exists(pid)
        return EXITED if exited else None


def _exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


RUNNERS = {"local": LocalRunner}


def get(name):
    try:
        return RUNNERS[name]()
    except KeyError:
        raise ImproperlyConfigured(f"Unknown trovi-launcher runner {name!r}")
