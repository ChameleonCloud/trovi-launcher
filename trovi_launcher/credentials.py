from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from keystoneauth1 import exceptions as keystone_exceptions
from keystoneauth1 import session
from keystoneauth1.identity import v3

TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
# How Chameleon's Keystones federate with its Keycloak
IDENTITY_PROVIDER = "chameleon"
PROTOCOL = "openid"
PROJECT_DOMAIN = "chameleon"

SITES_URL = "https://api.chameleoncloud.org/sites.json"
DEV_KEYCLOAK = "auth.dev.chameleoncloud.org"
# The sites dev Keycloak authenticates for, which the sites API doesn't list
DEV_SITES = [
    {"name": "CHI_DEV_UC", "uid": "uc", "web": "https://dev.uc.chameleoncloud.org"},
    {"name": "CHI_DEV", "uid": "tacc", "web": "https://dev.tacc.chameleoncloud.org"},
]
# Keystones whose Keycloak client isn't named keystone-<uid>-<prod|dev>
AUDIENCES = {"ncar": "keystone-ncar", "nrp": "keystone-nrp", "nu": "keystone-nu"}


def sites():
    """Return the sites users can launch on, by region name."""
    return cache.get_or_set("trovi_launcher:sites", _fetch_sites, 60 * 60)


def _fetch_sites():
    if urlparse(settings.OIDC_OP_TOKEN_ENDPOINT).hostname == DEV_KEYCLOAK:
        env, items = "dev", DEV_SITES
    else:
        response = requests.get(SITES_URL, timeout=10)
        response.raise_for_status()
        # Only bare metal sites take the node reservations launches make
        env, items = "prod", [
            site
            for site in response.json()["items"]
            if site["site_class"] == "baremetal"
        ]
    return {
        site["name"]: {
            "auth_url": f"{site['web']}:5000/v3",
            "audience": AUDIENCES.get(site["uid"], f"keystone-{site['uid']}-{env}"),
        }
        for site in items
    }


def resolve(launch, subject_token):
    """Return the OS_* environment the provisioner authenticates with."""
    site = sites().get(launch.site)
    if not site:
        raise ValidationError({"site": f"Unknown site {launch.site}."})
    auth = v3.OidcAccessToken(
        auth_url=site["auth_url"],
        identity_provider=IDENTITY_PROVIDER,
        protocol=PROTOCOL,
        access_token=_exchange_token(subject_token, site["audience"]),
        project_name=launch.project,
        project_domain_name=PROJECT_DOMAIN,
    )
    keystone = session.Session(auth=auth, timeout=30)
    try:
        token, project_id = keystone.get_token(), keystone.get_project_id()
    except (keystone_exceptions.Unauthorized, keystone_exceptions.Forbidden):
        raise PermissionDenied(
            f"You don't have access to project {launch.project} at {launch.site}."
        )
    return {
        "OS_AUTH_TYPE": "v3token",
        "OS_AUTH_URL": site["auth_url"],
        "OS_TOKEN": token,
        "OS_PROJECT_ID": project_id,
        "OS_REGION_NAME": launch.site,
        "OS_INTERFACE": "public",
    }


def _exchange_token(subject_token, audience):
    """Trade the user's token for one Keystone accepts, as the host's OIDC client."""
    if not subject_token:
        raise PermissionDenied("Launching requires the user's access token.")
    response = requests.post(
        settings.OIDC_OP_TOKEN_ENDPOINT,
        data={
            "grant_type": TOKEN_EXCHANGE,
            "client_id": settings.OIDC_RP_CLIENT_ID,
            "client_secret": settings.OIDC_RP_CLIENT_SECRET,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN,
            "requested_token_type": ACCESS_TOKEN,
            "audience": audience,
        },
        timeout=30,
    )
    if response.status_code in (400, 401, 403):
        raise PermissionDenied(f"Keycloak refused the token exchange: {response.text}")
    response.raise_for_status()
    return response.json()["access_token"]
