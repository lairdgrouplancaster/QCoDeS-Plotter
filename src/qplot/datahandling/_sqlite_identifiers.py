"""SQLite identifier escaping, independent of connections and reader policy."""


def quote_identifier(identifier: str) -> str:
    """Quote one identifier verbatim, including any embedded double quotes.

    Dotted names are a single identifier, not a qualified SQL expression.
    Callers remain responsible for validation at their own input boundary;
    query values should use SQL parameters instead of this helper.
    """
    return '"' + identifier.replace('"', '""') + '"'


def quote_identifier_text(identifier: object) -> str:
    """Preserve the snapshot readers' conversion of identifier values to text."""
    return quote_identifier(str(identifier))
