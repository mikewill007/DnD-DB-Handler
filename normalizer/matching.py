"""Conservative comparison within a name/edition/ruleset/type candidate group.

Callers establish that identity scope first. These functions compare gameplay
values; they do not decide whether different editions or documents can merge.
"""
import re
import unicodedata


def canonical_name(name):
    """Normalize Unicode, case, apostrophes, and spacing without fuzzy matching."""
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', name).casefold().replace('’', "'")).strip()


def differences(a, b):
    """Return populated disagreements as field -> [left value, right value]."""
    # Missing values are retained as missing, never treated as a contradiction.
    ignored = {'name', 'description', 'subcategory', 'category'}
    result = {k: [a[k], b[k]] for k in a.keys() & b.keys()
              if k not in ignored and a[k] is not None and b[k] is not None and a[k] != b[k]}
    # Magical effects are gameplay data. Text variants need review, not automatic merging.
    if a['record_type'] == 'magic_item' and a.get('description') != b.get('description'):
        result['description'] = [a.get('description'), b.get('description')]
    return result


def equivalent(a, b):
    """Require both compatible populated fields and shared matching evidence."""
    if differences(a, b):
        return False
    # Absence of disagreement alone is insufficient for two sparse records.
    evidence = {'cost_value', 'weight_lb', 'damage_dice', 'armor_class', 'description'}
    return any(a.get(k) is not None and a.get(k) == b.get(k) for k in evidence)
