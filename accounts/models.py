from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """A registered submitter.

    Login is by email (see ACCOUNT_LOGIN_METHODS), so `email` carries the
    uniqueness constraint that `username` would normally have. `username` is
    still required, but only as the public display name shown against a
    listing — it is never used to authenticate.
    """

    email = models.EmailField("email address", unique=True)

    def __str__(self):
        return self.username or self.email

    @property
    def display_name(self):
        return self.username or self.email.split("@")[0]

    @property
    def is_moderator(self):
        return self.is_superuser or self.groups.filter(name="Moderators").exists()
