"""
Money primitives.

Every cedi amount in this system is a Decimal quantized to 2 places with
ROUND_HALF_UP. Float arithmetic is never used for money, and division is
never allowed to silently drop pesewas -- use `allocate` for that.
"""
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

TWO_PLACES = Decimal('0.01')
ZERO = Decimal('0.00')


def D(value, default=None):
    """
    Coerce anything (str, int, float, Decimal, None) into a Decimal.

    Floats go through str() first so 0.1 does not become 0.1000000000000000055.
    Returns `default` (or raises) on garbage input.
    """
    if isinstance(value, Decimal):
        return value
    if value is None or value == '':
        if default is not None:
            return D(default)
        return ZERO
    try:
        return Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, ValueError, TypeError):
        if default is not None:
            return D(default)
        raise ValueError(f"Not a valid amount: {value!r}")


def q2(value):
    """Quantize to 2 decimal places, ROUND_HALF_UP (the way humans round cash)."""
    return D(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def pct(amount, percentage):
    """`percentage` percent of `amount`, rounded to the pesewa."""
    return q2(D(amount) * D(percentage) / Decimal('100'))


def allocate(total, weights):
    """
    Split `total` across len(weights) buckets in proportion to `weights`,
    with zero rounding loss: sum(result) == q2(total) exactly.

    Used when a PAIR price has to be spread across the individual pieces
    that were actually pulled from stock batches. Splitting GHS 100.01
    across 2 pieces gives [50.01, 50.00] -- never [50.00, 50.00] with a
    pesewa quietly vanishing from the books.
    """
    total = q2(total)
    weights = [D(w) for w in weights]
    weight_sum = sum(weights)

    if not weights:
        return []
    if weight_sum == 0:
        # Degenerate: give everything to the first bucket.
        return [total] + [ZERO] * (len(weights) - 1)

    parts = []
    running = ZERO
    for weight in weights[:-1]:
        part = q2(total * weight / weight_sum)
        parts.append(part)
        running += part
    # Last bucket absorbs the rounding remainder.
    parts.append(q2(total - running))
    return parts
