"""Stands in for the provisioner in tests and development.

FAKE_PROVISIONER_DELAY is how long each step takes, in seconds, and
FAKE_PROVISIONER_FAIL_AT names a step to fail.
"""

import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from trovi_launcher.provisioner import status

IP = "192.0.2.10"


def provision(launch):
    lease_id = launch.params.get("lease_id") or str(uuid.uuid4())
    server_id = str(uuid.uuid4())
    for step in ["lease", "server", "network"]:
        launch.enter(step, f"Faking {step}")
        time.sleep(float(os.environ.get("FAKE_PROVISIONER_DELAY", "0")))
        if step == os.environ.get("FAKE_PROVISIONER_FAIL_AT"):
            raise RuntimeError(f"Injected failure in {step}")
        if step == "lease" and not launch.params.get("lease_id"):
            launch.track("lease", lease_id)
        if step == "server":
            launch.track("server", server_id)
    end = datetime.now(timezone.utc) + timedelta(hours=1)
    return {
        "floating_ip": IP,
        "server_id": server_id,
        "lease_id": lease_id,
        "lease_end": end.isoformat(),
    }


def delete(kind, resource_id):
    pass


if __name__ == "__main__":
    sys.exit(status.run(provision, delete))
