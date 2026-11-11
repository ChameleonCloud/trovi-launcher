SECRET_KEY = "tests"
INSTALLED_APPS = ["trovi_launcher"]
MIDDLEWARE = ["django.middleware.csrf.CsrfViewMiddleware"]
ROOT_URLCONF = "trovi_launcher.tests.urls"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
STATIC_URL = "static/"

TROVI_LAUNCHER = {
    "SITES": {
        "CHI@UC": {"auth_url": "https://chi.uc.test:5000/v3", "audience": "keystone-uc"}
    },
    "CALLBACK_BASE_URL": "http://launcher.test",
}
OIDC_OP_TOKEN_ENDPOINT = "https://keycloak.test/token"
OIDC_RP_CLIENT_ID = "portal"
OIDC_RP_CLIENT_SECRET = "secret"
