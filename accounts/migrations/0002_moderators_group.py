"""Create the Moderators group.

`User.is_moderator` tests membership of a group called exactly "Moderators", so
the group has to exist before anyone can be put in it. Creating it here rather
than documenting it means a fresh instance has a working moderation queue after
`migrate` and `createsuperuser`, with no setup step nobody reads.
"""

from django.db import migrations

GROUP_NAME = "Moderators"


def create_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.get_or_create(name=GROUP_NAME)


def drop_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name=GROUP_NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(create_group, drop_group)]
