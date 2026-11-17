"""Keep unexpected API failures readable by approved browser origins."""
from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import get_settings


async def server_error_response(request: Request, _exc: Exception) -> JSONResponse:
    # Starlette generates unhandled-error responses outside user middleware,
    # including CORSMiddleware. Retain the same explicit origin allowlist on
    # that response; the server still logs and re-raises the original failure.
    headers = {"Cache-Control": "no-store", "Vary": "Origin"}
    origin = request.headers.get("origin")
    if origin and origin in get_settings().cors_origins_list:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected server error occurred."},
        headers=headers,
    )
