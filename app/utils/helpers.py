"""
Common utility functions shared across the application.

Includes date helpers, currency formatting, percentage calculations,
and other small reusable pieces.
"""


def pct_change(old: float, new: float) -> float:
    """Calculate percentage change from old to new value."""
    if old == 0:
        return 0.0
    return ((new - old) / old) * 100.0


def currency_fmt(value: float) -> str:
    """Format a float as a USD currency string."""
    return f"${value:,.2f}"
