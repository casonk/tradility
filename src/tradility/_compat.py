"""Names that reached the standard library after Python 3.10, which this package still supports."""

from datetime import timezone

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

UTC = timezone.utc  # datetime.UTC is 3.11+

__all__ = ["UTC", "tomllib"]
