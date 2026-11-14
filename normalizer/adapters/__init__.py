"""Translate importer payloads into equipment records without editing the inputs.

Each adapter yields zero or more (locator, normalized_record, attribution) tuples.
An empty locator denotes the whole payload. Nested documents may yield many
entries; the caller stores their complete original parent JSON as provenance.
"""
import html
import json
import re
from fractions import Fraction


# Shared value conversion and edition/document scoping.


def label(value):
    if isinstance(value, dict):
        return value.get('name') or value.get('value') or value.get('key')
    return value


def number(value):
    """Parse plain numbers, fractions, or supported unit suffixes; do not guess.

    This extracts a numeric value, not a unit conversion. Adapter callers retain
    currencies and check weight/range units before assigning normalized fields.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        match = re.fullmatch(r'\s*([\d,]+(?:\.\d+)?|\d+/\d+)\s*(?:lb\.?|gp|sp|cp|pp|ep)?\s*', value)
        if match:
            try:
                return float(Fraction(match[1].replace(',', '')))
            except (ValueError, ZeroDivisionError):
                pass
    return None


def text(value):
    """Create comparable plain text; original markup remains in source provenance."""
    if isinstance(value, list):
        value = '\n'.join(str(x) for x in value)
    if not isinstance(value, str):
        return None
    return re.sub(r'\s+', ' ', html.unescape(re.sub('<[^>]+>', ' ', value))).strip() or None


def base(obj, edition, source, document=None, ruleset=None):
    """Create shared fields and the scope within which cross-source matching is allowed."""
    doc = document or {}
    game = doc.get('gamesystem') or {}
    explicit = str(edition or '')
    if explicit not in ('2014', '2024'):
        explicit = {'5e-2014': '2014', '5e-2024': '2024'}.get(game.get('key'), 'unknown')
    key = doc.get('key', '')
    # Only recognized SRD documents or an adapter's explicit mapping share a
    # cross-provider scope. Unidentified editions stay scoped to source/document.
    scope = ruleset or (f'dnd5e:{explicit}' if key in ('srd-2014', 'srd-2024') else f'{source}:{key or "unspecified"}')
    if explicit == 'unknown':
        scope = f'{source}:{key or "unspecified"}:unknown'
    return {'name': html.unescape(obj.get('name', '')).strip(), 'edition': explicit,
            'ruleset': scope, 'record_type': 'item', 'description': text(obj.get('desc') or obj.get('description'))}


# 5e-bits and Open5e API payloads.


def api(obj, collection, edition, source):
    """Normalize equipment collections, including Open5e's embedded subtype objects."""
    if collection not in ('equipment', 'magic-items', 'items', 'magicitems', 'weapons', 'armor'):
        return
    doc = obj.get('document') or {}
    r = base(obj, edition, source, doc, f'dnd5e:{edition}' if source == 'five_e_bits' else None)
    # 5e-bits 2014 has singular category fields; 2024 uses category arrays.
    categories = obj.get('equipment_categories') or []
    category = label(obj.get('equipment_category') or obj.get('category'))
    category_keys = {c.get('index') for c in categories if isinstance(c, dict)}
    r['category'] = category
    r['subcategory'] = obj.get('weapon_category') or obj.get('armor_category')
    r['record_type'] = ('magic_item' if collection in ('magic-items', 'magicitems') else
                        'weapon' if collection == 'weapons' or obj.get('weapon_category') or obj.get('weapon') or 'weapons' in category_keys else
                        'armor' if collection == 'armor' or obj.get('armor_category') or obj.get('armor') or 'armor' in category_keys else 'item')
    cost = obj.get('cost')
    r['cost_value'] = number(cost.get('quantity')) if isinstance(cost, dict) else number(cost)
    r['cost_unit'] = cost.get('unit') if isinstance(cost, dict) else ('gp' if cost is not None else None)
    r['weight_lb'] = number(obj.get('weight')) if obj.get('weight_unit', 'lb') == 'lb' else None
    weapon = obj.get('weapon') or obj
    if isinstance(weapon, dict):
        damage = weapon.get('damage') or {}
        r['damage_dice'] = weapon.get('damage_dice') or damage.get('damage_dice')
        dt = label(weapon.get('damage_type') or damage.get('damage_type'))
        r['damage_type'] = dt.casefold() if isinstance(dt, str) else None
        props, mastery = [], []
        # Open5e mixes mastery properties with ordinary weapon properties.
        for p in weapon.get('properties', []):
            prop = p.get('property', p) if isinstance(p, dict) else p
            name = label(prop)
            if not name:
                continue
            (mastery if isinstance(prop, dict) and prop.get('type') == 'Mastery' else props).append(name.casefold())
            if name.casefold() == 'versatile' and isinstance(p, dict):
                r['versatile_damage'] = p.get('detail')
        if 'properties' in weapon:
            r['properties'] = sorted(props)
        if mastery:
            r['mastery'] = ', '.join(sorted(mastery))
        r['versatile_damage'] = r.get('versatile_damage') or (weapon.get('two_handed_damage') or {}).get('damage_dice')
        rng = weapon.get('range')
        r['range_normal'] = number(rng.get('normal')) if isinstance(rng, dict) else number(rng)
        r['range_long'] = number(rng.get('long')) if isinstance(rng, dict) else number(weapon.get('long_range'))
        if source == 'open5e':
            # A zero range denotes an unspecified range in these Open5e payloads;
            # treating it as literal zero would create false melee-range conflicts.
            r['range_normal'] = r['range_normal'] or None
            r['range_long'] = r['range_long'] or None
        if weapon.get('mastery'):
            r['mastery'] = str(label(weapon['mastery'])).casefold()
    armor = obj.get('armor') or obj
    if isinstance(armor, dict):
        ac = armor.get('armor_class') or {}
        r.update(armor_class=armor.get('ac_base', ac.get('base')),
                 dex_bonus=armor.get('ac_add_dexmod', ac.get('dex_bonus')),
                 dex_limit=armor.get('ac_cap_dexmod', ac.get('max_bonus')),
                 stealth_disadvantage=armor.get('grants_stealth_disadvantage', armor.get('stealth_disadvantage')),
                 strength_min=armor.get('strength_score_required', armor.get('str_minimum')))
    r['rarity'] = label(obj.get('rarity'))
    r['attunement'] = obj.get('requires_attunement')
    yield '', r, doc


# Foundry item payloads and property abbreviations.

FOUNDRY_PROPERTIES = {'amm': 'ammunition', 'fin': 'finesse', 'hvy': 'heavy', 'lgt': 'light',
                      'lod': 'loading', 'rch': 'reach', 'ret': 'returning', 'spc': 'special',
                      'thr': 'thrown', 'two': 'two-handed', 'ver': 'versatile', 'mgc': 'magical'}


def foundry(obj, collection, edition, source):
    """Normalize actual equipment items, excluding folder and other document types."""
    system = obj.get('system')
    if not isinstance(system, dict) or obj.get('type') not in ('weapon', 'equipment', 'consumable', 'tool', 'loot', 'container'):
        return
    origin = system.get('source') or {}
    if not isinstance(origin, dict):
        origin = {'book': origin}
    edition = origin.get('rules') or edition
    # Explicit rules fields only: pack suffixes are not edition evidence.
    doc = {'key': origin.get('book') or '', **origin}
    r = base(obj, edition, source, doc,
             f'dnd5e:{edition}' if not origin.get('book') and edition in ('2014', '2024') else None)
    rarity = system.get('rarity')
    armor = system.get('armor') or {}
    r['record_type'] = ('magic_item' if rarity or 'mgc' in system.get('properties', []) else
                        'weapon' if obj['type'] == 'weapon' else
                        'armor' if armor.get('value') is not None else 'item')
    r['category'] = obj['type']
    r['subcategory'] = label(system.get('type'))
    r['description'] = text((system.get('description') or {}).get('value'))
    price = system.get('price') or {}
    r['cost_value'], r['cost_unit'] = number(price.get('value')), price.get('denomination')
    weight = system.get('weight')
    r['weight_lb'] = (number(weight.get('value')) if weight.get('units') == 'lb' else None) if isinstance(weight, dict) else number(weight)
    damage = system.get('damage') or {}
    dmg = damage.get('base') or {}
    # Support structured damage and the older [formula, damage_type] part layout.
    if dmg.get('number') and dmg.get('denomination'):
        r['damage_dice'] = f"{dmg['number']}d{dmg['denomination']}"
        r['damage_type'] = ', '.join(sorted(dmg.get('types', []))) or None
    elif damage.get('parts'):
        part = damage['parts'][0]
        if isinstance(part, list):
            r['damage_dice'], r['damage_type'] = part[:2]
    versatile = damage.get('versatile')
    if isinstance(versatile, dict):
        versatile = f"{versatile['number']}d{versatile['denomination']}" if versatile.get('number') and versatile.get('denomination') else None
    r['versatile_damage'] = versatile or None
    r['properties'] = sorted(FOUNDRY_PROPERTIES.get(p, p) for p in system.get('properties', []))
    r['mastery'] = system.get('mastery') or None
    rng = system.get('range') or {}
    if rng.get('units', 'ft') == 'ft':
        r['range_normal'], r['range_long'] = number(rng.get('value')), number(rng.get('long'))
    # Foundry can store default armor values on weapons; only equipment contributes AC.
    r.update(armor_class=number(armor.get('value')) if obj['type'] == 'equipment' else None, dex_limit=number(armor.get('dex')) if obj['type'] == 'equipment' else None,
             stealth_disadvantage=system.get('stealth'), strength_min=number(system.get('strength')),
             rarity=rarity or None, attunement=system.get('attunement') in ('required', 1, 2) if system.get('attunement') not in (None, '') else None)
    yield '', r, doc


# BTMorton nested equipment tables and magic-item prose.


def pointer_part(key):
    """Escape one JSON Pointer path component, including names containing '/' or '~'."""
    return str(key).replace('~', '~0').replace('/', '~1')


def btmorton(obj, collection, edition, source):
    """Extract entries from the legacy SRD's column-oriented tables and headings.

    This dataset is explicitly mapped to 2014. Equipment locators ending in
    /table/N are derived zero-based row locators, not literal JSON Pointers into
    the column arrays. Magic-item locators are JSON Pointers to their objects.
    """
    if collection not in ('04 equipment', '10 magic items'):
        return
    def walk(node, path=''):
        if isinstance(node, dict):
            table = node.get('table')
            if isinstance(table, dict) and 'Cost' in table:
                key = next((k for k in ('Armor', 'Name', 'Item', 'Goods') if k in table), None)
                # Expenses and animals are not equipment entries.
                if key and '/Expenses/' not in path and 'Mounts and Other Animals' not in path:
                    for i, name in enumerate(table[key]):
                        if not name or name.startswith('*'):
                            continue
                        # Reconstruct each row from parallel columns. Italic-only
                        # category headings above are skipped rather than made items.
                        row = {k: v[i] if i < len(v) else None for k, v in table.items() if isinstance(v, list)}
                        r = base({'name': name}, '2014', source, ruleset='dnd5e:2014')
                        r['record_type'] = 'weapon' if key == 'Name' and '/Weapons/' in path else 'armor' if key == 'Armor' else 'item'
                        r['subcategory'] = path.rsplit('/', 1)[-1]
                        r['cost_value'], r['weight_lb'] = number(row.get('Cost')), number(row.get('Weight'))
                        unit = re.search(r'\b(cp|sp|ep|gp|pp)\b', row.get('Cost') or '')
                        r['cost_unit'] = unit[1] if unit else None
                        damage = re.fullmatch(r'(\d+d\d+|\d+) (\w+)', row.get('Damage') or '')
                        if damage:
                            r['damage_dice'], r['damage_type'] = damage.groups()
                        properties = row.get('Properties') or ''
                        if properties and '\ufffd' not in properties:
                            # Damaged replacement characters are not reliable properties.
                            r['properties'] = sorted(re.sub(r'\s*\(.*?\)', '', properties).casefold().split(', '))
                        versatile = re.search(r'versatile \((\d+d\d+)\)', properties, re.I)
                        if versatile:
                            r['versatile_damage'] = versatile[1]
                        rng = re.search(r'range (\d+)/(\d+)', properties)
                        if rng:
                            r['range_normal'], r['range_long'] = map(int, rng.groups())
                        ac = row.get('Armor Class (AC)', '')
                        acmatch = re.search(r'\d+', ac)
                        if acmatch:
                            r['armor_class'] = int(acmatch[0])
                            r['dex_bonus'] = 'Dex' in ac
                            limit = re.search(r'max (\d+)', ac)
                            r['dex_limit'] = int(limit[1]) if limit else None
                            r['stealth_disadvantage'] = row.get('Stealth') == 'Disadvantage'
                            strength = re.search(r'\d+', row.get('Strength') or '')
                            r['strength_min'] = int(strength[0]) if strength else None
                        yield path + '/table/' + str(i), r, {'edition_evidence': 'BTMorton legacy SRD dataset'}
            # A top-level magic-item entry begins with an italic category/rarity
            # heading. Other nested prose/tables should not create duplicate items.
            if path == '/Magic Items':
                for name, value in node.items():
                    if not isinstance(value, dict) or 'content' not in value:
                        continue
                    content = value['content']
                    heading = content[0] if isinstance(content, list) and content else content
                    if not isinstance(heading, str) or not heading.startswith('*'):
                        continue
                    r = base({'name': name, 'desc': content}, '2014', source, ruleset='dnd5e:2014')
                    r['record_type'] = 'magic_item'
                    rarity = re.search(r'\b(very rare|uncommon|common|rare|legendary|artifact)\b', heading, re.I)
                    r['rarity'] = rarity[0].casefold() if rarity else None
                    r['attunement'] = 'requires attunement' in heading.casefold()
                    yield path + '/' + pointer_part(name), r, {'edition_evidence': 'BTMorton legacy SRD dataset'}
            for key, value in node.items():
                if key != 'table':
                    yield from walk(value, path + '/' + pointer_part(key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from walk(value, path + '/' + str(i))
    yield from walk(obj)


# Provider dispatch.


def extract(source, obj, collection, edition):
    """Yield adapter records; adkinn has no equipment in the current imported data."""
    adapter = {'five_e_bits': api, 'open5e': api, 'foundry_dnd5e': foundry,
               'btmorton_srd': btmorton, 'adkinn_srd521': lambda *args: iter(())}[source]
    yield from adapter(obj, collection, edition, source)
