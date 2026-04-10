WEB_DIRECTORY = "./web/js"
NODE_CLASS_MAPPINGS = {}
__all__ = ["NODE_CLASS_MAPPINGS", "WEB_DIRECTORY"]

from . import routes  # noqa: F401, E402 — registers server routes
