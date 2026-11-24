import json

from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import service
from .models import Launch


@csrf_exempt
@require_POST
def events(request, launch_id):
    launch = get_object_or_404(Launch, pk=launch_id)
    token = request.headers.get("Authorization", "").removeprefix("Bearer ")
    try:
        event = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "Body must be JSON"}, status=400)
    try:
        service.record_event(launch, token, event)
    except ValidationError as e:
        return JsonResponse({"error": e.messages}, status=400)
    return HttpResponse(status=204)
