"""Mail settings in the admin, and the buttons that prove them.

What matters: with nothing saved, mail goes exactly where the environment
said it did before; with an SMTP host saved, every message — including the
ones that name `DEFAULT_FROM_EMAIL`, which is fixed at start-up — goes
through the saved server as the saved sender; passwords are never shown back;
and only a superuser can see any of it, because outbound mail carries every
password-reset link the site sends.

pytest-django swaps `EMAIL_BACKEND` for locmem in every test, so the tests
here call the configured backend by name.
"""

import imaplib

import pytest
from django.conf import settings
from django.core import mail
from django.core.mail import EmailMessage, get_connection
from django.urls import reverse

from core import mail as core_mail
from core.models import MailConfig
from submissions import inbound

BACKEND = "core.mail.ConfiguredEmailBackend"


@pytest.fixture(autouse=True)
def delivery_is_locmem(settings):
    settings.EMAIL_DELIVERY_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.DEFAULT_FROM_EMAIL = "noreply@env.example"
    settings.SERVER_EMAIL = "noreply@env.example"


@pytest.fixture
def fake_smtp(monkeypatch):
    """Stand in for the SMTP backend; record how it was built and what it sent."""
    record = {"kwargs": None, "sent": [], "fail": None}

    class FakeSMTP:
        def __init__(self, **kwargs):
            record["kwargs"] = kwargs

        def open(self):
            return True

        def close(self):
            pass

        def send_messages(self, messages):
            if record["fail"]:
                raise record["fail"]
            record["sent"].extend(messages)
            return len(messages)

    monkeypatch.setattr(core_mail, "SMTPBackend", FakeSMTP)
    return record


def send(**kwargs):
    message = EmailMessage(
        subject="Hello", body="Body", to=["reader@example.com"],
        connection=get_connection(BACKEND), **kwargs,
    )
    return message.send()


def save(**fields):
    config = MailConfig.load()
    for name, value in fields.items():
        setattr(config, name, value)
    config.save()
    return config


# --- the backend ------------------------------------------------------------


@pytest.mark.django_db
def test_with_nothing_saved_mail_goes_where_the_environment_says(fake_smtp):
    send()
    [sent] = mail.outbox
    assert sent.from_email == f"{settings.SITE_NAME} <noreply@env.example>"
    assert fake_smtp["kwargs"] is None


@pytest.mark.django_db
def test_a_bare_sender_gets_the_site_name_and_a_named_one_keeps_its_own():
    # Seen live: a bare address shows in the inbox as "noreply".
    save(from_email="hello@saved.example")
    send(from_email="noreply@env.example")
    assert mail.outbox[0].from_email == f"{settings.SITE_NAME} <hello@saved.example>"


@pytest.mark.django_db
def test_a_saved_smtp_host_carries_everything(fake_smtp):
    save(smtp_host="smtp.saved.example", smtp_port=465, smtp_use_tls=False,
         smtp_use_ssl=True, smtp_user="u", smtp_password="p")
    send()
    assert mail.outbox == []
    assert fake_smtp["kwargs"]["host"] == "smtp.saved.example"
    assert fake_smtp["kwargs"]["port"] == 465
    assert fake_smtp["kwargs"]["use_ssl"] is True
    assert fake_smtp["kwargs"]["use_tls"] is False
    assert (fake_smtp["kwargs"]["username"], fake_smtp["kwargs"]["password"]) == ("u", "p")
    assert len(fake_smtp["sent"]) == 1


@pytest.mark.django_db
def test_the_saved_sender_replaces_the_start_up_default():
    # allauth and the moderation mail both read DEFAULT_FROM_EMAIL directly.
    save(from_email="Town Events <hello@saved.example>")
    send(from_email="noreply@env.example")
    assert mail.outbox[0].from_email == "Town Events <hello@saved.example>"


@pytest.mark.django_db
def test_a_deliberate_other_sender_is_left_alone():
    save(from_email="hello@saved.example")
    send(from_email="someone@else.example")
    assert mail.outbox[0].from_email == "someone@else.example"


@pytest.mark.django_db
def test_the_mailbox_comes_from_the_admin_when_set(settings):
    settings.INBOUND_IMAP_HOST = "imap.env.example"
    settings.INBOUND_IMAP_USER = "env@example.com"
    assert inbound.mailbox().host == "imap.env.example"
    save(imap_host="imap.saved.example", imap_user="saved@example.com",
         inbound_address="events@saved.example")
    box = inbound.mailbox()
    assert (box.host, box.user, box.address) == (
        "imap.saved.example", "saved@example.com", "events@saved.example",
    )


# --- the admin ----------------------------------------------------------------


@pytest.fixture
def superuser(db, django_user_model):
    return django_user_model.objects.create_superuser(
        username="root", email="root@example.com", password="pw"
    )


@pytest.fixture
def admin_client(client, superuser):
    client.force_login(superuser)
    return client


CHANGE = reverse("admin:core_mailconfig_change", args=[1])


def form_data(**overrides):
    data = {
        "smtp_host": "smtp.saved.example", "smtp_port": "587",
        "smtp_use_tls": "on", "smtp_user": "u", "smtp_password": "",
        "from_email": "", "imap_host": "", "imap_port": "993",
        "imap_user": "", "imap_password": "", "imap_folder": "INBOX",
        "inbound_address": "",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


@pytest.mark.django_db
def test_a_moderator_with_staff_access_cannot_see_mail_settings(client, moderator):
    moderator.is_staff = True
    moderator.save()
    client.force_login(moderator)
    MailConfig.load()
    assert client.get(CHANGE).status_code in (302, 403)


@pytest.mark.django_db
def test_a_password_is_never_shown_back(admin_client):
    save(smtp_password="hunter2-secret")
    page = admin_client.get(CHANGE).content.decode()
    assert "hunter2-secret" not in page


@pytest.mark.django_db
def test_a_blank_password_keeps_the_stored_one(admin_client):
    save(smtp_password="kept")
    admin_client.post(CHANGE, form_data())
    assert MailConfig.load().smtp_password == "kept"


@pytest.mark.django_db
def test_a_new_password_replaces_it_and_clear_removes_it(admin_client):
    save(smtp_password="old")
    admin_client.post(CHANGE, form_data(smtp_password="new"))
    assert MailConfig.load().smtp_password == "new"
    admin_client.post(CHANGE, form_data(clear_smtp_password="on"))
    assert MailConfig.load().smtp_password == ""


@pytest.mark.django_db
def test_starttls_and_implicit_tls_together_are_refused(admin_client):
    response = admin_client.post(CHANGE, form_data(smtp_use_ssl="on"))
    assert response.status_code == 200
    assert "Choose one" in response.content.decode()


# --- the Test buttons -----------------------------------------------------------


@pytest.mark.django_db
def test_send_a_test_email_to_the_person_pressing_it(admin_client, fake_smtp):
    save(smtp_host="smtp.saved.example")
    response = admin_client.post(
        reverse("admin:core_mailconfig_test_outbound"), follow=True
    )
    [sent] = fake_smtp["sent"]
    assert sent.to == ["root@example.com"]
    assert "Sent a test message to root@example.com via SMTP smtp.saved.example" in response.content.decode()


@pytest.mark.django_db
def test_a_failed_send_says_why(admin_client, fake_smtp):
    import smtplib

    save(smtp_host="smtp.saved.example")
    fake_smtp["fail"] = smtplib.SMTPAuthenticationError(535, b"Authentication failed")
    response = admin_client.post(
        reverse("admin:core_mailconfig_test_outbound"), follow=True
    )
    body = response.content.decode()
    assert "failed" in body and "SMTPAuthenticationError" in body


@pytest.mark.django_db
def test_the_test_buttons_only_answer_a_post(admin_client, fake_smtp):
    save(smtp_host="smtp.saved.example")
    admin_client.get(reverse("admin:core_mailconfig_test_outbound"))
    assert fake_smtp["sent"] == []


class FakeImap:
    def __init__(self, unseen=b"4 5 6", folder_ok=True):
        self.unseen = unseen
        self.folder_ok = folder_ok
        self.flagged = False
        self.selected_readonly = None

    def select(self, folder, readonly=False):
        self.selected_readonly = readonly
        return ("OK" if self.folder_ok else "NO"), [b""]

    def uid(self, command, *args):
        if command == "STORE":
            self.flagged = True
        return "OK", [self.unseen]

    def logout(self):
        pass


@pytest.mark.django_db
def test_testing_the_mailbox_counts_without_reading(monkeypatch, admin_client):
    save(imap_host="imap.saved.example", imap_user="events@saved.example")
    box = FakeImap()
    monkeypatch.setattr(inbound, "_connect", lambda b: box)
    response = admin_client.post(
        reverse("admin:core_mailconfig_test_inbound"), follow=True
    )
    assert "has 3 unread messages" in response.content.decode()
    assert box.selected_readonly is True
    assert not box.flagged


@pytest.mark.django_db
def test_a_bad_password_is_reported_in_the_servers_words(monkeypatch):
    save(imap_host="imap.saved.example", imap_user="events@saved.example")

    def refuse(box):
        raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")

    ok, sentence = inbound.test_mailbox(connect=refuse)
    assert not ok
    assert "AUTHENTICATIONFAILED" in sentence and "b'" not in sentence


@pytest.mark.django_db
def test_a_missing_folder_is_named():
    save(imap_host="imap.saved.example", imap_user="u", imap_folder="Events")
    ok, sentence = inbound.test_mailbox(connect=lambda b: FakeImap(folder_ok=False))
    assert not ok and "Events" in sentence


@pytest.mark.django_db
def test_an_unconfigured_mailbox_says_so(settings):
    settings.INBOUND_IMAP_HOST = ""
    ok, sentence = inbound.test_mailbox(connect=lambda b: pytest.fail("connected"))
    assert not ok and "No IMAP host" in sentence
