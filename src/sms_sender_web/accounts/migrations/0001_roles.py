from django.db import migrations

ROLES = ("viewer", "operator", "admin")


def create_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in ROLES:
        Group.objects.get_or_create(name=name)


class Migration(migrations.Migration):
    """The three role groups (spec 4.12)."""

    dependencies = [("auth", "0012_alter_user_first_name_max_length")]

    operations = [migrations.RunPython(create_roles, migrations.RunPython.noop)]
