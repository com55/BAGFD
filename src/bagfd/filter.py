"""Filename matching for game-file queries.

`FileFilter` wraps a single pattern + matching strategy. The strategy is one of
`FilterMethod` (defined in `bagfd.enums`); passing `FilterMethod.AUTO` (the
default) inspects the pattern and picks `GLOB`, `REGEX`, or `CONTAINS`
automatically.

`FilterMethod` is a `StrEnum`, so a plain string such as ``"glob"`` is accepted
anywhere a `FilterMethod` is — they compare equal.
"""
import fnmatch
import re

from .enums import FilterMethod


class FileFilter:
    """A compiled filename matcher.

    Args:
        pattern: The pattern to match filenames against.
        filter_method: Matching strategy. ``FilterMethod.AUTO`` (default) picks
            a concrete method from the pattern's characters.
    """

    def __init__(self, pattern: str, filter_method: FilterMethod = FilterMethod.AUTO):
        if filter_method == FilterMethod.AUTO:
            filter_method = self.auto_detect(pattern)
        self.pattern = pattern
        # Normalise a bare string ("glob") into the enum so `matches` can compare
        # against FilterMethod members reliably.
        self.filter_method = FilterMethod(filter_method)

    def matches(self, name: str) -> bool:
        """Return True if ``name`` matches this filter."""
        if self.filter_method == FilterMethod.GLOB:
            return fnmatch.fnmatch(name, self.pattern)
        elif self.filter_method == FilterMethod.REGEX:
            return bool(re.search(self.pattern, name))
        elif self.filter_method == FilterMethod.STARTS_WITH:
            return name.startswith(self.pattern)
        elif self.filter_method == FilterMethod.ENDS_WITH:
            return name.endswith(self.pattern)
        else:  # FilterMethod.CONTAINS
            return self.pattern in name

    @staticmethod
    def auto_detect(pattern: str) -> FilterMethod:
        """Guess a matching strategy from the characters in ``pattern``.

        Glob metacharacters win first, then regex metacharacters, otherwise the
        pattern is treated as a plain substring.
        """
        if any(c in pattern for c in ('*', '?', '[')):
            return FilterMethod.GLOB
        if any(c in pattern for c in ('^', '$', '\\', '+', '|', '(')):
            return FilterMethod.REGEX
        return FilterMethod.CONTAINS
