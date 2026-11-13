"""Provision one Chameleon bare metal node with python-chi.

Run as python -m trovi_launcher.provisioner.chameleon. status.py handles the
contract with the launcher; this module creates and deletes the resources.

It understands these arguments, and ignores any others:

- ssh_public_key: required; the user logs in as cc with it.
- lease_id: launch on this existing lease. Without it, node_type and
  lease_duration_hours are required, and a new lease is made.
- image: the disk image, by default python-chi's default image.
- contents: the access methods of the artifact version's contents, as Trovi
  lists them. At boot, cloud-init clones the git one into cc's home directory
  or, without one, downloads and unpacks the http one. The launch doesn't wait
  for that or report its failure; /var/log/cloud-init-output.log on the node
  has why.
"""

import base64
import json
import shlex
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import chi.clients
import chi.exception
import chi.lease
import chi.network
import chi.server
import novaclient.exceptions

from . import status

# How soon a user-supplied lease must start for the launch to wait for it
LEASE_START_WINDOW = timedelta(minutes=15)
LEASE_TIMEOUT = 20 * 60
SERVER_TIMEOUT = 30 * 60
SSH_TIMEOUT = 20 * 60
# The kinds of resource provision tracks, and how to delete one by its ID
DELETE: dict[str, Callable[[str], object]] = {
    "lease": lambda lease_id: chi.lease.get_lease(lease_id).delete(),
    "keypair": lambda name: chi.clients.nova().keypairs.delete(name),
    "server": lambda server_id: chi.clients.nova().servers.delete(server_id),
}
# What deleting a resource that's already gone raises, such as a server deleted
# with its lease. python-chi raises CHIValueError for a lease Blazar can't find.
GONE = (chi.exception.CHIValueError, novaclient.exceptions.NotFound)
# Runs on the node with the http access method as its argument. It's Python,
# which cloud-init images always have, unlike curl or unzip, and sticks to what
# Python 3.8 has for older images.
DOWNLOAD = r"""
import json, os, re, shutil, sys, tarfile, tempfile, zipfile
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

method = json.loads(sys.argv[1])
name = os.path.basename(unquote(urlparse(method["url"]).path)) or "contents"
request = Request(method["url"], headers=method["headers"], method=method["method"])
with urlopen(request) as response, tempfile.NamedTemporaryFile(delete=False) as f:
    shutil.copyfileobj(response, f)
directory = re.sub(r"\.(zip|tgz|tar|tar\.gz)$", "", name)
if zipfile.is_zipfile(f.name):
    with zipfile.ZipFile(f.name) as archive:
        archive.extractall(directory)
elif tarfile.is_tarfile(f.name):
    with tarfile.open(f.name) as archive:
        archive.extraction_filter = getattr(tarfile, "data_filter", None)
        archive.extractall(directory)
else:
    shutil.move(f.name, name)
    sys.exit()
os.remove(f.name)
"""


def provision(launch: status.Launch) -> dict[str, str]:
    """Bring up one node for the launch and give the user SSH access to it.

    Returns:
        The launch's result, which the launcher stores and shows to the user:

        - floating_ip: the node's public address.
        - server_id: the Nova server ID.
        - lease_id: the lease the node runs on.
        - lease_end: when that lease ends, as an ISO 8601 UTC time.
    """
    params: dict[str, Any] = launch.params
    name = f"trovi-{params['id']}"
    if not params.get("ssh_public_key"):
        raise ValueError("Set ssh_public_key so you can log in to the node")

    launch.enter("lease", "Reserving a node")
    lease = reserve(launch, name)

    launch.enter("keypair", "Adding your SSH key")
    chi.server.update_keypair(name, params["ssh_public_key"])
    launch.track("keypair", name)

    launch.enter("server", "Launching the server")
    server = create_server(launch, name, lease)
    launch.delete("keypair", name)

    launch.enter("network", "Attaching a floating IP")
    ip = attach_floating_ip(server, lease)

    launch.enter("access", f"Waiting for SSH on {ip}")
    server.check_connectivity(host=ip, timeout=SSH_TIMEOUT, show=None)
    launch.report(message=f"ssh cc@{ip}")
    return {
        "floating_ip": ip,
        "server_id": server.id,
        "lease_id": lease.id,
        "lease_end": _utc(lease.end_date).isoformat(),
    }


def delete(kind: str, resource_id: str) -> None:
    """Delete one resource that provision tracked, unless it's already gone."""
    try:
        DELETE[kind](resource_id)
    except GONE:
        pass


def reserve(launch: status.Launch, name: str) -> chi.lease.Lease:
    """Return an active lease with a node and a floating IP reservation.

    With lease_id, this uses the user's lease, waiting for it if it starts
    within LEASE_START_WINDOW. The user's lease is not tracked, so it is never
    deleted. Otherwise it reserves one node_type node and one floating IP for
    lease_duration_hours, and tracks the new lease.
    """
    params: dict[str, Any] = launch.params
    if params.get("lease_id"):
        lease = chi.lease.get_lease(params["lease_id"])
        start = _utc(lease.start_date)
        now = datetime.now(timezone.utc)
        if lease.status != "ACTIVE" and start > now + LEASE_START_WINDOW:
            raise RuntimeError(
                f"Lease {lease.id} starts at {start:%Y-%m-%d %H:%M} UTC; "
                "launch again once it has started"
            )
    else:
        if not (params.get("node_type") and params.get("lease_duration_hours")):
            raise ValueError("Set node_type and lease_duration_hours, or lease_id")
        hours = timedelta(hours=int(params["lease_duration_hours"]))
        lease = chi.lease.Lease(name, duration=hours)
        lease.add_node_reservation(amount=1, node_type=params["node_type"])
        lease.add_fip_reservation(amount=1)
        lease.submit(wait_for_active=False)
        launch.track("lease", lease.id)
    lease.wait(status="active", show=None, timeout=LEASE_TIMEOUT)
    if lease.status != "ACTIVE":
        raise RuntimeError(f"Lease {lease.id} is {lease.status}")
    if not (lease.node_reservations and lease.fip_reservations):
        raise RuntimeError(
            f"Lease {lease.id} needs a node and a floating IP reservation"
        )
    return lease


def create_server(
    launch: status.Launch, name: str, lease: chi.lease.Lease
) -> chi.server.Server:
    """Launch the node on the lease's node reservation and wait until it's up.

    The server is named name and uses the keypair of the same name, which
    holds the user's key. With contents, it fetches them at boot. It is
    tracked as soon as it has an ID. If it ends in ERROR, this raises with
    Nova's fault message, such as "No valid host was found".
    """
    server = chi.server.Server(
        name,
        reservation_id=lease.node_reservations[0]["id"],
        image_name=launch.params.get("image") or chi.server.DEFAULT_IMAGE,
        key_name=name,
    )
    options = {}
    if command := fetch_command(launch.params.get("contents", [])):
        options["user_data"] = user_data(command)
    server.submit(wait_for_active=False, show=None, **options)
    server.refresh()
    launch.track("server", server.id)
    server.wait(show=None, timeout=SERVER_TIMEOUT)
    if server.status != "ACTIVE":
        fault = getattr(chi.clients.nova().servers.get(server.id), "fault", {})
        raise RuntimeError(
            f"Server {server.id} is {server.status}: {fault.get('message', '')}"
        )
    return server


def attach_floating_ip(server: chi.server.Server, lease: chi.lease.Lease) -> str:
    """Attach an unused floating IP reserved by the lease, and return its address.

    Blazar tags the floating IPs it reserves with "reservation:<id>".
    """
    tag = f"reservation:{lease.fip_reservations[0]['id']}"
    free = [
        fip["floating_ip_address"]
        for fip in chi.network.list_floating_ips()
        if tag in fip["tags"] and not fip["port_id"]
    ]
    if not free:
        raise RuntimeError(f"Lease {lease.id} has no unused reserved floating IP")
    server.associate_floating_ip(free[0])
    return free[0]


def user_data(command: str) -> str:
    """Return a boot script that runs command as cc, encoded for Nova.

    cloud-init runs it as root on the node's first boot. Nova takes user data
    base64-encoded, and openstacksdk's compute proxy doesn't encode it.
    """
    script = f"#!/bin/sh\nsu - cc -c {shlex.quote(command)}\n"
    return base64.b64encode(script.encode()).decode()


def fetch_command(methods: list[dict[str, Any]]) -> Optional[str]:
    """Return a shell command that fetches contents into the home directory.

    With a git method, it clones the remote into a directory named after the
    repository and checks out the method's ref. Otherwise, with an http method,
    it downloads the URL and unpacks a zip or tar archive into a directory
    named after the file; any other file is left as downloaded. Returns None
    when there is neither.
    """
    first = {}
    for method in methods:
        first.setdefault(method["protocol"], method)
    if git := first.get("git"):
        remote = git["remote"]
        directory = shlex.quote(
            remote.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
        )
        command = f"git clone -- {shlex.quote(remote)} {directory}"
        if ref := git.get("ref"):
            command += f" && git -C {directory} checkout {shlex.quote(ref)}"
        return command
    if http := first.get("http"):
        return shlex.join(["python3", "-c", DOWNLOAD, json.dumps(http)])
    return None


def _utc(value: datetime) -> datetime:
    # python-chi parses Blazar's timestamps as naive UTC
    return value.replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    sys.exit(status.run(provision, delete))
