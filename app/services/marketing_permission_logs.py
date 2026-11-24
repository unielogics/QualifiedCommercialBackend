"""Mask the signed preference capability in HTTP access and database-conflict logs."""
import logging

_PREFIX = "/api/v1/public/email-marketing/permission/"


class _PermissionPathFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            # Uvicorn and our conflict handler both log the path as a format argument.
            for index, arg in enumerate(record.args):
                if isinstance(arg, str) and arg.startswith(_PREFIX):
                    route = arg[len(_PREFIX):].partition("?")[0]
                    _, _, action = route.partition("/")
                    suffix = "/" + action if action in {"confirm", "decline"} else ""
                    record.args = (*record.args[:index], _PREFIX + "[redacted]" + suffix, *record.args[index + 1:])
        return True


def protect_permission_logs() -> None:
    for name in ("uvicorn.access", "qc"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _PermissionPathFilter) for f in logger.filters):
            logger.addFilter(_PermissionPathFilter())
