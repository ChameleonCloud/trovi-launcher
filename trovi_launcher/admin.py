from django.contrib import admin

from . import service
from .models import Launch, LaunchEvent


class LaunchEventInline(admin.TabularInline):
    model = LaunchEvent
    fields = readonly_fields = ["created_at", "step", "level", "message", "data"]
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Launch)
class LaunchAdmin(admin.ModelAdmin):
    list_display = [
        "id",
        "artifact_title",
        "status",
        "step",
        "site",
        "username",
        "created_at",
    ]
    list_filter = ["status", "site", "runner", "created_at"]
    search_fields = ["user_sub", "username", "email", "artifact_title"]
    exclude = ["callback_token_hash"]
    inlines = [LaunchEventInline]
    actions = ["cancel", "reconcile"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if launch := self.get_object(request, object_id):
            service.reconcile(launch)
        return super().change_view(request, object_id, form_url, extra_context)

    @admin.action(description="Cancel selected launches")
    def cancel(self, request, queryset):
        for launch in queryset:
            service.cancel_launch(launch)

    @admin.action(description="Reconcile selected launches with their provisioner")
    def reconcile(self, request, queryset):
        for launch in queryset:
            service.reconcile(launch)
