"""Independent Redis coordination for MD5 content preprocessing."""

from dovideo.application.content_context import ContentContextKey
from .redis import RedisTaskLock


class RedisContentBuildLock(RedisTaskLock):
    """Reuse SET NX PX/token-checked Lua, with a separate content namespace."""
    def __init__(self, client, *, ttl_ms=120_000):
        super().__init__(client, ttl_ms=ttl_ms, prefix="lock:content-context")

    def redis_key(self, key: ContentContextKey):
        return f"{self.prefix}:{key.digest}"
