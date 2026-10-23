"""Recipe matching: which recipe does an event get? Pure functions.

Match rules, per key (any metadata field, plus scan_number and status):
    700            equal
    [600, 800]     inclusive range (two numbers)
    [FERMAT, RASTER]  one of (anything else that is a list)
A missing value never matches: a recipe that asks for energy needs an energy.
"""


def event_values(event):
    """Everything a recipe may match on."""
    values = dict(event.metadata)
    values.update(scan_number=event.scan_number, status=event.status)
    return values


def _rule_matches(rule, value):
    if isinstance(rule, list):
        if len(rule) == 2 and all(isinstance(x, (int, float)) for x in rule):
            return isinstance(value, (int, float)) and rule[0] <= value <= rule[1]
        return value in rule
    return value == rule


def why_not(recipe, values):
    """None if the recipe matches, else a short reason."""
    for key, rule in recipe.match.items():
        value = values.get(key)
        if value is None:
            return "%s unknown" % key
        if not _rule_matches(rule, value):
            return "%s=%s not in %s" % (key, _fmt(value), rule)
    return None


def select(recipes, event):
    """(recipe, None) for the first match, or (None, explanation of every miss)."""
    values = event_values(event)
    misses = []
    for recipe in recipes:
        reason = why_not(recipe, values)
        if reason is None:
            return recipe, None
        misses.append("%s: %s" % (recipe.name, reason))
    return None, "no recipe matches (" + "; ".join(misses) + ")"


def _fmt(value):
    return ("%.4g" % value) if isinstance(value, float) else str(value)
