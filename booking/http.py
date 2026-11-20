import base64
import binascii
import hashlib
import hmac
from functools import wraps
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

# A verified Basic header is remembered for a short time, so repeated API calls
# do not pay for the deliberately slow password hash on every request.
AUTH_CACHE_SECONDS = 60


def error(code, status):
    return JsonResponse({"code": code, "detail": code}, status=status)


def _cache_key(header):
    digest = hmac.new(settings.SECRET_KEY.encode(), header.encode(), hashlib.sha256).hexdigest()
    return "roomlab-basic:" + digest


def basic_user(request, header):
    """Return the active user for a Basic Authorization header, or None."""
    key = _cache_key(header)
    cached = cache.get(key)
    if cached is not None:
        pk, username, password_hash = cached
        user = get_user_model().objects.filter(pk=pk, username=username, is_active=True).first()
        # A changed password (for example after seed_demo) invalidates the entry.
        if user is not None and hmac.compare_digest(user.password, password_hash):
            return user
        cache.delete(key)
    scheme, _, payload = header.partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        raw = base64.b64decode(payload, validate=True).decode()
        name, password = raw.split(":", 1)
    except (UnicodeError, ValueError, binascii.Error):
        return None
    user = authenticate(request, username=name, password=password)
    if user is not None:
        cache.set(key, (user.pk, user.get_username(), user.password), AUTH_CACHE_SECONDS)
    return user


def endpoint(method):
    """Local demo API. Server assigns request.user from Basic credentials."""
    def decorate(view):
        @csrf_exempt
        @wraps(view)
        def authenticated(request, *args, **kwargs):
            if request.method != method:
                return error("method_not_allowed", 405)
            header = request.headers.get("Authorization", "")
            user = basic_user(request, header) if header else None
            if user is None:
                response = error("unauthenticated", 401)
                response["WWW-Authenticate"] = 'Basic realm="roomlab"'
                return response
            request.user = user
            return view(request, *args, **kwargs)
        return authenticated
    return decorate


def record(row):
    return {"id": row.pk, "room_id": row.room_id, "start": row.start.isoformat(),
            "end": row.end.isoformat(), "status": row.status}
