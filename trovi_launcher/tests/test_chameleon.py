import importlib.util
from datetime import datetime, timedelta, timezone
from unittest import TestCase, mock, skipUnless

from trovi_launcher.provisioner import status

HAS_CHI = importlib.util.find_spec("chi") is not None
if HAS_CHI:
    from trovi_launcher.provisioner import chameleon

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 user@host"


def params(**overrides):
    return {
        "id": "abc",
        "node_type": "compute_skylake",
        "image": "CC-Ubuntu24.04",
        "lease_duration_hours": 2,
        "lease_id": "",
        "ssh_public_key": KEY,
        "contents_remote": "https://github.com/org/repo.git",
        "contents_ref": "abc123",
    } | overrides


def lease(lease_status="ACTIVE", starts_in=0):
    start = datetime.now(timezone.utc) + timedelta(minutes=starts_in)
    return mock.Mock(
        id="lease",
        status=lease_status,
        start_date=start.replace(tzinfo=None),
        end_date=datetime(2026, 11, 7, 12),
        node_reservations=[{"id": "node-res"}],
        fip_reservations=[{"id": "fip-res"}],
    )


@skipUnless(HAS_CHI, "python-chi is not installed")
class ChameleonTests(TestCase):
    def setUp(self):
        self.events = []
        patches = {
            "lease": mock.patch("chi.lease.Lease", return_value=lease()),
            "get_lease": mock.patch("chi.lease.get_lease"),
            "server": mock.patch("chi.server.Server"),
            "keypair": mock.patch("chi.server.update_keypair"),
            "floating_ips": mock.patch(
                "chi.network.list_floating_ips",
                return_value=[
                    {
                        "floating_ip_address": "192.0.2.1",
                        "tags": ["reservation:fip-res"],
                        "port_id": "p",
                    },
                    {
                        "floating_ip_address": "192.0.2.2",
                        "tags": ["reservation:fip-res"],
                        "port_id": None,
                    },
                    {"floating_ip_address": "192.0.2.3", "tags": [], "port_id": None},
                ],
            ),
            "nova": mock.patch("chi.clients.nova"),
            "generate": mock.patch("paramiko.RSAKey.generate"),
            "post_event": mock.patch.object(status, "post_event", self.events.append),
        }
        self.mocks = {}
        for name, patcher in patches.items():
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.server = self.mocks["server"].return_value
        self.server.id = "server"
        self.server.status = "ACTIVE"
        self.connection = self.server.ssh_connection.return_value.__enter__.return_value
        self.mocks["generate"].return_value.get_name.return_value = "ssh-rsa"
        self.mocks["generate"].return_value.get_base64.return_value = "AAAA"

    def provision(self, **overrides):
        self.launch = status.Launch(params(**overrides), chameleon.delete)
        return chameleon.provision(self.launch)

    def test_provisions_node(self):
        result = self.provision()

        self.assertEqual(
            result,
            {
                "floating_ip": "192.0.2.2",
                "server_id": "server",
                "lease_id": "lease",
                "lease_end": "2026-11-07T12:00:00+00:00",
            },
        )
        self.mocks["lease"].assert_called_once_with(
            "trovi-abc", duration=timedelta(hours=2)
        )
        new_lease = self.mocks["lease"].return_value
        new_lease.add_node_reservation.assert_called_once_with(
            amount=1, node_type="compute_skylake"
        )
        new_lease.add_fip_reservation.assert_called_once_with(amount=1)
        self.mocks["keypair"].assert_called_once_with("trovi-abc", "ssh-rsa AAAA")
        self.mocks["server"].assert_called_once_with(
            "trovi-abc",
            reservation_id="node-res",
            image_name="CC-Ubuntu24.04",
            key_name="trovi-abc",
        )
        self.server.associate_floating_ip.assert_called_once_with("192.0.2.2")
        self.mocks["nova"].return_value.keypairs.delete.assert_called_once_with(
            "trovi-abc"
        )
        self.assertEqual(
            self.launch.created, [("lease", "lease"), ("server", "server")]
        )
        self.assertEqual(
            [call.args[0] for call in self.connection.run.call_args_list],
            [
                "git clone -- https://github.com/org/repo.git repo"
                " && git -C repo checkout abc123",
                f"echo '{KEY}' > ~/.ssh/authorized_keys",
            ],
        )

    def test_uses_user_lease(self):
        self.mocks["get_lease"].return_value = lease()
        self.provision(lease_id="lease")

        self.mocks["get_lease"].assert_called_once_with("lease")
        self.mocks["lease"].assert_not_called()
        self.assertEqual(self.launch.created, [("server", "server")])

    def test_rejects_user_lease_starting_later(self):
        self.mocks["get_lease"].return_value = lease("PENDING", starts_in=120)
        with self.assertRaisesRegex(RuntimeError, "launch again"):
            self.provision(lease_id="lease")

    def test_requires_what_it_needs_before_creating_anything(self):
        for overrides, message in [
            ({"node_type": ""}, "Set node_type"),
            ({"lease_duration_hours": None}, "Set node_type"),
            ({"ssh_public_key": ""}, "Set ssh_public_key"),
        ]:
            with self.subTest(overrides), self.assertRaisesRegex(ValueError, message):
                self.provision(**overrides)
        self.mocks["lease"].assert_not_called()

    def test_converts_lease_length_from_text(self):
        self.provision(lease_duration_hours="3")

        self.mocks["lease"].assert_called_once_with(
            "trovi-abc", duration=timedelta(hours=3)
        )

    def test_skips_contents_without_a_repository(self):
        self.provision(contents_remote="")

        self.assertEqual(self.connection.run.call_count, 1)
        self.assertNotIn("contents", [event["step"] for event in self.events])

    def test_rejects_lease_in_error(self):
        self.mocks["lease"].return_value = lease("ERROR")
        with self.assertRaisesRegex(RuntimeError, "Lease lease is ERROR"):
            self.provision()

        self.assertEqual(self.launch.created, [("lease", "lease")])

    def test_server_error_includes_fault(self):
        self.server.status = "ERROR"
        nova = self.mocks["nova"].return_value
        nova.servers.get.return_value.fault = {"message": "No valid host"}
        with self.assertRaisesRegex(RuntimeError, "ERROR: No valid host"):
            self.provision()

    def test_contents_failure_is_a_warning(self):
        self.connection.run.side_effect = [RuntimeError("clone failed"), None]
        self.provision()

        self.assertIn(
            {
                "step": "contents",
                "level": "warning",
                "message": "Could not load contents: clone failed",
            },
            self.events,
        )

    def test_deletes_by_kind(self):
        chameleon.delete("lease", "l")
        chameleon.delete("server", "s")

        self.mocks["get_lease"].return_value.delete.assert_called_once_with()
        self.mocks["get_lease"].assert_called_once_with("l")
        self.mocks["nova"].return_value.servers.delete.assert_called_once_with("s")
