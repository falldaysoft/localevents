import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

from django.utils.csp import CSP

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env for local development. In production every value comes from the
# environment (see the Helm chart), so this is a no-op there.
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("SECRET_KEY", "insecure-dev-key-change-me")

DEBUG = os.environ.get("DEBUG", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Instance identity
#
# Everything that makes this deployment *a specific town* lives here, driven by
# environment variables with deliberately generic defaults. Nothing downstream
# should hardcode a place name — that is what makes this project reusable by
# another community without forking the code. See README.md.
#
# Soft content (about text, submission guidelines, footer links) is editable in
# the admin instead, via core.models.SiteConfig.
# ---------------------------------------------------------------------------

SITE_NAME = os.environ.get("SITE_NAME", "Local Events")
SITE_TAGLINE = os.environ.get("SITE_TAGLINE", "What's on near you")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "hello@localhost")

# Emails contain links, and a background task has no request to derive a
# hostname from. Explicit here rather than via django.contrib.sites because a
# wrong value produces links that silently go nowhere — better to set it beside
# the rest of the instance identity than to discover it in the Sites table.
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "http://localhost:8000")

# Map defaults. MAP_BBOX is "min_lat,min_lng,max_lat,max_lng" and doubles as the
# region gate: geocoded events landing outside it get flagged for review rather
# than silently published, which is what keeps a local site local.
MAP_CENTER_LAT = float(os.environ.get("MAP_CENTER_LAT", "0"))
MAP_CENTER_LNG = float(os.environ.get("MAP_CENTER_LNG", "0"))
MAP_ZOOM = int(os.environ.get("MAP_ZOOM", "12"))
MAP_BBOX = [float(v) for v in os.environ.get("MAP_BBOX", "-90,-180,90,180").split(",")]

# OpenStreetMap tiles need no API key, which keeps the no-build-step promise.
# An instance with real traffic can point these at MapTiler/Protomaps instead.
TILE_URL = os.environ.get("TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
TILE_ATTRIBUTION = os.environ.get(
    "TILE_ATTRIBUTION",
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
)

# Identifies us to Nominatim and to sites we fetch for enrichment. Their usage
# policies require a contactable UA, so this is not merely cosmetic.
USER_AGENT = os.environ.get("USER_AGENT", f"{SITE_NAME} bot ({CONTACT_EMAIL})")


# ---------------------------------------------------------------------------
# Core Django
# ---------------------------------------------------------------------------

ALLOWED_HOSTS = os.environ.get(
    "ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]"
).split(",")

CSRF_TRUSTED_ORIGINS = [
    o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.contrib.humanize",
    # Background tasks: Django 6 provides the `django.tasks` API, this package
    # provides the DatabaseBackend and the `db_worker` command.
    "django_tasks",
    "django_tasks.backends.database",
    # Accounts
    "allauth",
    "allauth.account",
    "allauth.mfa",
    # Project
    "accounts",
    "content",
    "core",
    "enrichment",
    "events",
    "moderation",
    "submissions",
    "web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    # Must stay below the CSP middleware: responses travel back up this list,
    # so this runs first on the way out and can widen the policy before it is
    # written. See core.middleware.SiteHeadCSPMiddleware.
    "core.middleware.SiteHeadCSPMiddleware",
]

ROOT_URLCONF = "localevents.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.csp",
                "core.context_processors.site",
                "core.context_processors.site_head",
                "accounts.context_processors.claim",
                "content.context_processors.footer_pages",
            ],
        },
    },
]

WSGI_APPLICATION = "localevents.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}" if DEBUG else None,
    )
}

# An unreachable database must fail fast, not hang. libpq waits indefinitely by
# default, so a stalled server — a wedged volume under it, say — blocks the
# request past gunicorn's worker timeout and the worker is killed mid-connect.
# Code that means to degrade gracefully never gets its exception, because a
# hang is not an exception. Five seconds is far longer than a healthy connect
# and far shorter than any probe budget. Postgres only: SQLite rejects it.
if DATABASES["default"].get("ENGINE", "").endswith("postgresql"):
    DATABASES["default"].setdefault("OPTIONS", {}).setdefault(
        "connect_timeout", int(os.environ.get("DB_CONNECT_TIMEOUT", "5"))
    )

AUTH_USER_MODEL = "accounts.User"

SITE_ID = 1

LANGUAGE_CODE = "en-ca"
TIME_ZONE = os.environ.get("SITE_TIMEZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # The manifest backend requires collectstatic to have run, which the
        # Docker build does. In development it would mean re-running
        # collectstatic after every dependency change just to load a page, so
        # fall back to plain storage there.
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        ),
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Pages and media
#
# Uploaded images are normalised and stored as rows, not files — see
# content/models.py for why, and content/images.py for what "normalised" does.
# These bound what a single upload can cost in database bytes and in RAM while
# it is being processed, so they are settings rather than constants: an
# instance with a beefier pod and a taste for large photographs can raise them
# without a fork.
#
# The defaults aim at "a photo on a page read on a phone". 1600px is enough for
# a full-width image on a 2x display; the WebP quality is the point where the
# next step up costs real bytes for a difference nobody sees.
# ---------------------------------------------------------------------------

CMS_MAX_UPLOAD_BYTES = int(os.environ.get("CMS_MAX_UPLOAD_BYTES", 12 * 1024 * 1024))
# Checked from the image header before anything decodes it. 50 megapixels is
# far beyond any camera a volunteer owns and far below what it takes to
# exhaust a pod's memory.
CMS_MAX_UPLOAD_PIXELS = int(os.environ.get("CMS_MAX_UPLOAD_PIXELS", 50_000_000))
CMS_IMAGE_MAX_DIMENSION = int(os.environ.get("CMS_IMAGE_MAX_DIMENSION", 1600))
CMS_IMAGE_QUALITY = int(os.environ.get("CMS_IMAGE_QUALITY", 82))
CMS_THUMBNAIL_MAX_DIMENSION = int(os.environ.get("CMS_THUMBNAIL_MAX_DIMENSION", 400))
CMS_THUMBNAIL_QUALITY = int(os.environ.get("CMS_THUMBNAIL_QUALITY", 70))


# ---------------------------------------------------------------------------
# Accounts
#
# Email is the login identifier; username is required but purely a public
# display name (it is what appears as "submitted by" on a listing). Email
# confirmation is mandatory — it is the main thing standing between the
# submission queue and drive-by spam.
# ---------------------------------------------------------------------------

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "username*", "password1*", "password2*"]
# Only to call `username` a display name, in the label and in the message for a
# collision — allauth's own wording names a field this site does not have.
ACCOUNT_FORMS = {"signup": "accounts.forms.SignupForm"}
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_UNIQUE_EMAIL = True
# Tell someone their address is already registered, instead of allauth's
# default of accepting the signup, quietly creating nothing, and mailing the
# existing account about it. That default protects against using the signup
# page to test whether a given person is a member — a real concern, and the
# wrong trade here: the email address *is* the credential on this site, so
# "that address already has an account, sign in" is the whole answer someone
# needs, and a signup that appears to succeed and then does not is how people
# end up locked out of a listing they cannot find. The cost is accepted
# knowingly: this page will confirm whether an address is registered, and so
# will password reset, which the same setting governs.
ACCOUNT_PREVENT_ENUMERATION = False
ACCOUNT_EMAIL_SUBJECT_PREFIX = f"[{SITE_NAME}] "
ACCOUNT_RATE_LIMITS = {
    "login_failed": "5/5m",
    "signup": "10/h",
    "reset_password": "5/h",
    "confirm_email": "5/h",
}

# Passkeys are offered as an additional method, not a replacement — the email
# address still has to be confirmed either way.
#
# Passkeys only, deliberately. A TOTP app is a second step *in addition to* a
# password, which is a security ritual to ask of someone whose account can post
# a church bazaar to a listings page; a passkey replaces the password with a
# fingerprint and is less work than what it replaces. Offering both put an
# "Authenticator App / Activate" panel in front of people who came to set up
# Touch ID. Recovery codes stay out for the same reason — they only exist to
# rescue a second factor there is now no way to be locked out by.
MFA_SUPPORTED_TYPES = ["webauthn"]
MFA_PASSKEY_LOGIN_ENABLED = True
MFA_PASSKEY_SIGNUP_ENABLED = False

LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"


# ---------------------------------------------------------------------------
# Email
#
# Console backend in dev; in production point EMAIL_HOST at an SMTP relay. SES
# exposes one (email-smtp.<region>.amazonaws.com:587) so no extra dependency is
# needed — the credentials are IAM SMTP credentials, not an AWS access key.
# ---------------------------------------------------------------------------

EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", f"noreply@{ALLOWED_HOSTS[0]}")
SERVER_EMAIL = DEFAULT_FROM_EMAIL


# ---------------------------------------------------------------------------
# Background tasks
#
# Enrichment, geocoding, feed polling and outbound mail all run here rather than
# in a request. Locally that means a second process: `manage.py db_worker`.
# ---------------------------------------------------------------------------

TASKS = {
    "default": {
        "BACKEND": "django_tasks.backends.database.DatabaseBackend",
    }
}


# ---------------------------------------------------------------------------
# LLM enrichment
#
# These are fallbacks only. The live configuration is an admin-editable
# singleton (core.models.AIConfig) so the endpoint, model and spend cap can be
# changed without a redeploy.
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# Seeds the admin singleton the first time it is created. Changing it later is
# an admin edit, not a redeploy — the point of keeping model choice in the
# database is being able to compare models against recorded costs.
DEFAULT_OPENROUTER_MODEL = os.environ.get(
    "OPENROUTER_MODEL", "anthropic/claude-sonnet-5"
)


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# The ingress terminates TLS, so Django only learns the original scheme from
# this header. WebAuthn origin checks depend on getting it right.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Everything we load is either same-origin or a known CDN. Tailwind's CDN build
# generates styles at runtime, hence 'unsafe-inline' for styles; scripts are
# nonce-based. Map tiles and user-submitted event images are remote, so img-src
# stays permissive.
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [
        CSP.SELF,
        CSP.NONCE,
        "https://cdn.tailwindcss.com",
        "https://cdn.jsdelivr.net",
        "https://unpkg.com",
    ],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE, "https://fonts.googleapis.com", "https://unpkg.com"],
    "font-src": [CSP.SELF, "https://fonts.gstatic.com"],
    "img-src": [CSP.SELF, "data:", "https:"],
    "connect-src": [CSP.SELF, "https://nominatim.openstreetmap.org"],
    "frame-ancestors": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
}


# ---------------------------------------------------------------------------
# Logging
#
# Django's default configuration only logs to stderr when DEBUG is on, which
# means production tracebacks vanish. This routes everything to stderr always.
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}
