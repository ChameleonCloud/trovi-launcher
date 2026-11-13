SECRET_KEY = "tests"
INSTALLED_APPS = ["trovi_launcher"]
MIDDLEWARE = ["django.middleware.csrf.CsrfViewMiddleware"]
ROOT_URLCONF = "trovi_launcher.tests.urls"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
STATIC_URL = "static/"

TROVI_LAUNCHER = {}
OIDC_OP_TOKEN_ENDPOINT = "https://keycloak.test/token"
OIDC_RP_CLIENT_ID = "portal"
OIDC_RP_CLIENT_SECRET = "secret"
