"""Keep response-owned files alive until streaming and cancellation finish."""
import anyio
from fastapi.responses import FileResponse


class OwnedFileResponse(FileResponse):
    def __init__(self, path, *, release, **kwargs):
        super().__init__(path, **kwargs)
        self.release = release

    async def __call__(self, scope, receive, send):
        # pathsend may defer reading beyond this call; use ordinary streaming.
        scope = {**scope, "extensions": {k: v for k, v in scope.get("extensions", {}).items()
                                         if k != "http.response.pathsend"}}
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.release(self.path)


def media_file_response(services, path, **kwargs):
    release = getattr(services, "release_response_file", None)
    if release is None:
        return FileResponse(path, **kwargs)
    return OwnedFileResponse(path, release=release, **kwargs)
