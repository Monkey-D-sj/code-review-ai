"""Qualified-name construction and parsing.

All code that builds or splits a qualified name must go through this module
so the separator conventions are defined in one place.

Format:
    module :: scope . scope . ... . name

Examples:
    auth                                      # module
    auth::UserService                         # class in module
    auth::UserService.authenticate            # method in class
    auth::login                               # function in module
"""

# Separators — change once, applies everywhere.
MODULE_SEP = "::"   # between module path and first scope
SCOPE_SEP  = "."    # between nested scopes (class → method etc.)


def join(module_qname: str, name: str, scope_qname: str | None = None) -> str:
    """Build a fully qualified name.

    join("auth", "login")              → "auth::login"
    join("auth", "authenticate", "auth::UserService") → "auth::UserService.authenticate"
    """
    if scope_qname:
        return f"{scope_qname}{SCOPE_SEP}{name}"
    return f"{module_qname}{MODULE_SEP}{name}"


def short(qname: str) -> str:
    """Return the last segment (the bare name)."""
    # Split on SCOPE_SEP first (inner), then MODULE_SEP.
    # e.g. "auth::UserService.authenticate" → "authenticate"
    return qname.rsplit(SCOPE_SEP, 1)[-1].rsplit(MODULE_SEP, 1)[-1]
