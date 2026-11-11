from unittest import mock

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, override_settings
from keystoneauth1 import exceptions as keystone_exceptions

from trovi_launcher import credentials
from trovi_launcher.models import Launch

SITES = {
    "CHI@UC": {"auth_url": "https://chi.uc.test:5000/v3", "audience": "keystone-uc"}
}


@override_settings(
    TROVI_LAUNCHER={"SITES": SITES},
    OIDC_OP_TOKEN_ENDPOINT="https://keycloak.test/token",
    OIDC_RP_CLIENT_ID="portal",
    OIDC_RP_CLIENT_SECRET="secret",
)
class ExchangeCredentialTests(SimpleTestCase):
    def setUp(self):
        self.launch = Launch(site="CHI@UC", project="CHI-000000")
        self.post = self.patch("trovi_launcher.credentials.requests.post")
        self.post.return_value.status_code = 200
        self.post.return_value.json.return_value = {"access_token": "exchanged"}
        self.auth = self.patch("trovi_launcher.credentials.v3.OidcAccessToken")
        session = self.patch("trovi_launcher.credentials.session.Session")
        self.keystone = session.return_value
        self.keystone.get_token.return_value = "keystone-token"
        self.keystone.get_project_id.return_value = "project-id"

    def patch(self, target):
        patcher = mock.patch(target)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_exchanges_for_project_token(self):
        env = credentials.resolve(self.launch, "user-token")

        self.assertEqual(
            env,
            {
                "OS_AUTH_TYPE": "v3token",
                "OS_AUTH_URL": "https://chi.uc.test:5000/v3",
                "OS_TOKEN": "keystone-token",
                "OS_PROJECT_ID": "project-id",
                "OS_REGION_NAME": "CHI@UC",
                "OS_INTERFACE": "public",
            },
        )
        data = self.post.call_args.kwargs["data"]
        self.assertEqual(data["subject_token"], "user-token")
        self.assertEqual(data["audience"], "keystone-uc")
        self.assertEqual(data["client_id"], "portal")
        self.auth.assert_called_once_with(
            auth_url="https://chi.uc.test:5000/v3",
            identity_provider="chameleon",
            protocol="openid",
            access_token="exchanged",
            project_name="CHI-000000",
            project_domain_name="chameleon",
        )

    def test_rejects_refused_exchange(self):
        self.post.return_value.status_code = 403
        with self.assertRaises(PermissionDenied):
            credentials.resolve(self.launch, "user-token")

    def test_rejects_non_member(self):
        self.keystone.get_token.side_effect = keystone_exceptions.Unauthorized()
        with self.assertRaises(PermissionDenied):
            credentials.resolve(self.launch, "user-token")

    def test_requires_token_and_known_site(self):
        with self.assertRaises(PermissionDenied):
            credentials.resolve(self.launch, None)
        with self.assertRaises(ValidationError):
            credentials.resolve(Launch(site="CHI@XX", project="p"), "user-token")
