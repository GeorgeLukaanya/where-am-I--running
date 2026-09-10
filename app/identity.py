"""Turn an instance's name into a colour.

The page is tinted by whichever container answered it, so that landing on a
different replica is visible before you have read a single character. The hue
has to be derived rather than assigned: nothing knows the container's ID until
it exists, and there is no registry to look it up in.

The same four lines run in the browser, so bars in the routing ledger match the
page tint of the instance they represent. Keep the two implementations
identical -- this is the one piece of logic that is deliberately duplicated.
"""


def accent_hue(hostname: str) -> int:
    """A stable hue in [0, 360) for this instance."""
    value = 0
    for char in hostname:
        value = (value * 31 + ord(char)) % 360_000
    return value % 360
