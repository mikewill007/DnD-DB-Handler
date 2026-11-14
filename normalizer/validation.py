"""Nonfatal equipment checks used to populate the master's review queue."""
import math


def issues(record):
    """Return review messages without changing or rejecting a normalized record.

    Adapters must supply numeric values or None for numeric fields. Missing
    optional fields are expected; only a few type-specific fields are required.
    """
    result = []
    # Unknown editions remain usable but must not be matched across source scopes.
    if record['edition'] == 'unknown':
        result.append('Unknown edition: kept separate by source and document')
    # Preserve suspicious source values and expose them for human review.
    for key in ('cost_value', 'weight_lb', 'range_normal', 'range_long'):
        value = record.get(key)
        if value is not None and (not math.isfinite(value) or value < 0):
            result.append(f'Invalid {key}: {value}')
    # Completeness checks depend on record type, not on provider-specific defaults.
    required = {'weapon': ('damage_dice', 'damage_type'), 'armor': ('armor_class',)}
    for key in required.get(record['record_type'], ()):
        if record.get(key) is None:
            result.append(f'Missing {key}')
    if record['record_type'] == 'magic_item' and not record.get('rarity'):
        result.append('Missing rarity')
    return result
