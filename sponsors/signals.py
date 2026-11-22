"""A paid placement stops when its event leaves the site, however it leaves.

On the model rather than in moderation's services because an event comes
down by more paths than those: the edit form's status field, the Django
admin, a delete. See `services.stop_placements`.
"""

from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver

from events.models import Event


@receiver(post_save, sender=Event)
def event_saved(sender, instance, raw=False, **kwargs):
    if raw or instance.status == Event.Status.PUBLISHED:
        return
    from . import services

    services.stop_placements(instance)


@receiver(pre_delete, sender=Event)
def event_deleted(sender, instance, **kwargs):
    from . import services

    services.stop_placements(instance, deleted=True)
