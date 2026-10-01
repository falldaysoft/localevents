from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import ApiToken, User


@admin.register(User)
class UserAdmin(UserAdmin):
    list_display = ("username", "email", "is_staff", "date_joined")
    search_fields = ("username", "email")


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "hint", "created_at", "last_used_at", "revoked_at")
    list_filter = ("revoked_at",)
    search_fields = ("name", "user__email")
    # The hash is useless to read and dangerous to edit; revoking is the only
    # change an administrator should make here.
    readonly_fields = ("user", "key_hash", "hint", "created_at", "last_used_at")

    def has_add_permission(self, request):
        # A token is only ever made by ApiToken.issue, which is the one place
        # the key exists in the clear; an admin-made row would have no key.
        return False
