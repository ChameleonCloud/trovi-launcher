"""Report a provisioner's progress to the launcher.

The launcher runs a provisioner as its own process (see PROVISIONER in the
TROVI_LAUNCHER setting). Any command that follows this contract can be one:

- LAUNCH_PARAMS is the launch as JSON: id, site and project, plus the
  launch's arguments. The arguments come from the artifact's defaults and the
  user's changes to them, and mean whatever the provisioner makes of them.
- LAUNCH_CLEANUP, if set, is a JSON list of [kind, id]: delete those
  resources instead of launching.
- OS_* are OpenStack credentials scoped to the launch's project.
- POST each event as JSON to CALLBACK_URL, with CALLBACK_TOKEN as a bearer
  token. An event has a status (running, then ready, failed, cancelled or
  deleted) and optionally a step, level, message, result (anything the user
  should see, such as an IP address) and data, which records each resource
  as {"created": {kind: id}} or {"deleted": {kind: id}}.
- SIGTERM cancels the launch. Exiting without a final status fails it.

A Python provisioner can call run() with its own provision and delete
functions, and leave the rest of the contract to this module.
"""

import json
import os
import signal
import sys
import time
from collections.abc import Callable
from types import FrameType
from typing import Any, NoReturn

import requests

# Deletes one resource, given its kind and ID
Delete = Callable[[str, str], None]


class Cancelled(BaseException):
    """Raised in the provisioner when the launcher cancels the launch.

    It's a BaseException, so library code that catches Exception can't
    swallow a cancel.
    """


def post_event(event: dict[str, Any]) -> None:
    """Send one event to the launcher, and print it to the provisioner's log.

    It tries three times. If they all fail the event is lost; if it was the
    final status, the launcher marks the launch failed once it sees that this
    process has exited.
    """
    print(json.dumps(event), flush=True)
    for attempt in range(1, 4):
        try:
            requests.post(
                os.environ["CALLBACK_URL"],
                json=event,
                headers={"Authorization": f"Bearer {os.environ['CALLBACK_TOKEN']}"},
                timeout=30,
            ).raise_for_status()
            return
        except OSError as e:
            print(f"Report attempt {attempt} failed: {e}", file=sys.stderr)
            time.sleep(2)


class Launch:
    """The launch being provisioned, and the resources created for it.

    params is LAUNCH_PARAMS: id, site and project, plus the launch's
    arguments. step is the step being worked on, which every report carries.
    created lists the (kind, id) of each resource that exists, oldest first.
    """

    def __init__(self, params: dict[str, Any], delete: Delete) -> None:
        self.params = params
        self.step = ""
        self.created: list[tuple[str, str]] = []
        self._delete = delete

    def enter(self, step: str, message: str) -> None:
        """Start a step; the user sees message as the launch's progress."""
        self.step = step
        self.report(status="running", message=message)

    def report(self, **event: Any) -> None:
        """Send an event for the current step.

        event can set status, level, message, result and data, as the module
        docstring describes. Without a status, the launch is still running.
        """
        post_event({"step": self.step} | event)

    def track(self, kind: str, resource_id: str) -> str:
        """Record that a resource now exists, and return its ID.

        Call this as soon as a resource is created. Everything tracked is
        deleted if the launch fails or is cancelled, and whatever is left when
        it finishes is what the user's clean up deletes.
        """
        self.created.append((kind, resource_id))
        self.report(
            message=f"Created {kind} {resource_id}",
            data={"created": {kind: resource_id}},
        )
        return resource_id

    def delete(self, kind: str, resource_id: str) -> None:
        """Delete a tracked resource now, such as a temporary keypair."""
        self._delete(kind, resource_id)
        self.created.remove((kind, resource_id))
        self.report(
            message=f"Deleted {kind} {resource_id}",
            data={"deleted": {kind: resource_id}},
        )

    def delete_created(self) -> None:
        """Delete every tracked resource, newest first.

        A resource that can't be deleted is reported as a warning and stays
        tracked.
        """
        for kind, resource_id in reversed(self.created):
            try:
                self.delete(kind, resource_id)
            except Exception as e:
                self.report(
                    level="warning",
                    message=f"Could not delete {kind} {resource_id}: {e}",
                )


# Creates a launch's resources, and returns its result
Provision = Callable[[Launch], dict[str, Any]]


def run(provision: Provision, delete: Delete) -> int:
    """Run the provisioner process: provision a launch, or clean one up.

    provision(launch) creates the resources, tracking each with launch.track,
    and returns the launch's result. If provision raises, or the launch is
    cancelled, everything tracked is deleted and the launch is reported
    failed or cancelled.

    delete(kind, resource_id) deletes one tracked resource. It must work from
    the ID alone, because a clean up runs in a later process.

    Returns:
        The process's exit status: 0 if the launch is ready or cleaned up,
        and 1 otherwise.
    """
    signal.signal(signal.SIGTERM, _cancel)
    launch = Launch(json.loads(os.environ["LAUNCH_PARAMS"]), delete)
    if cleanup := os.environ.get("LAUNCH_CLEANUP"):
        launch.created = [tuple(resource) for resource in json.loads(cleanup)]
        return _clean_up(launch)
    try:
        result = provision(launch)
    except Cancelled:
        launch.delete_created()
        launch.report(status="cancelled", message="Launch cancelled")
        return 1
    except Exception as e:
        launch.delete_created()
        launch.report(
            status="failed", level="error", message=f"{type(e).__name__}: {e}"
        )
        return 1
    launch.report(status="ready", result=result)
    return 0


def _clean_up(launch: Launch) -> int:
    """Delete the resources listed in LAUNCH_CLEANUP."""
    launch.enter("cleanup", "Deleting the launch's resources")
    launch.delete_created()
    if launch.created:
        launch.report(
            status="failed",
            level="error",
            message="Some resources could not be deleted",
        )
        return 1
    launch.report(status="deleted", message="Deleted the launch's resources")
    return 0


def _cancel(signum: int, frame: FrameType | None) -> NoReturn:
    raise Cancelled()
