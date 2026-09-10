"""Give every instance a stable colour.

An instance's colour has to be derived rather than assigned: nothing knows a
container's id until it exists, and there is no registry to look it up in. The
derivation picks a *slot* in a fixed, validated categorical palette rather than
an arbitrary hue -- two instances hashing to hues 10 degrees apart would be
indistinguishable as adjacent lines on a chart, which is precisely where the
colour has to do work.

Slots are assigned by the server, in /api/fleet, so the browser never has to
reimplement this.
"""

PALETTE_SIZE = 8


def accent_slot(hostname: str) -> int:
    """The palette slot this instance prefers, in [0, PALETTE_SIZE)."""
    value = 0
    for char in hostname:
        value = (value * 31 + ord(char)) % 360_000
    return value % PALETTE_SIZE


def assign_slots(hostnames) -> dict[str, int]:
    """Give each instance a distinct slot, preferring its own.

    Eight slots and five replicas collide more often than intuition suggests,
    and two identically coloured lines are worse than a line in an unexpected
    colour -- so a taken slot falls through to the next free one. Sorted input
    keeps the outcome stable: the same fleet always produces the same colours,
    however the addresses happened to arrive.
    """
    assigned: dict[str, int] = {}
    taken: set[int] = set()

    for hostname in sorted(hostnames):
        slot = accent_slot(hostname)
        while slot in taken and len(taken) < PALETTE_SIZE:
            slot = (slot + 1) % PALETTE_SIZE
        taken.add(slot)
        assigned[hostname] = slot

    return assigned
