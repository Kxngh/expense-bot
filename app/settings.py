"""Applies budget and category changes asked for in plain language.

The LLM only proposes changes; everything here is validated and scoped to the
person who asked.
"""
from . import categories, db
from .charts import money

MAX_BUDGET = 1e9


def apply(wa_id: str, s: dict) -> list:
    lines = []

    # categories first, so "add a Pets category with a 2000 budget" works in one message
    for raw in s.get("add_categories") or []:
        name = categories.clean_name(raw)
        if not name or name.upper() == db.TOTAL:
            continue
        existing = categories.resolve(name, wa_id)
        if existing and existing.lower() == name.lower():
            lines.append(f"• {existing} already exists")
            continue
        if len(db.custom_categories(wa_id)) >= categories.MAX_CUSTOM:
            lines.append(f"• Can't add {name}: you already have {categories.MAX_CUSTOM} custom categories")
            continue
        db.add_category(wa_id, name)
        lines.append(f"🏷️ Added category *{name}*")

    custom = {c.lower(): c for c in db.custom_categories(wa_id)}
    for raw in s.get("remove_categories") or []:
        name = categories.clean_name(raw)
        if name.lower() in custom:
            moved = db.remove_category(wa_id, custom[name.lower()])
            lines.append(f"🗑️ Removed *{custom[name.lower()]}*" + (f" ({moved} expenses moved to Other)" if moved else ""))
        elif name in categories.CATEGORIES:
            lines.append(f"• {name} is a built-in category and can't be removed")
        elif name:
            lines.append(f"• You don't have a category called {name}")

    allowed = set(categories.for_user(wa_id))
    for b in s.get("budgets") or []:
        cat = b.get("category")
        cat = db.TOTAL if str(cat).upper() == db.TOTAL else (cat if cat in allowed else categories.resolve(str(cat), wa_id))
        try:
            amount = float(b.get("amount"))
        except (TypeError, ValueError):
            continue
        if not cat or amount < 0 or amount > MAX_BUDGET:
            continue
        label = "Monthly total" if cat == db.TOTAL else cat
        db.set_budget(wa_id, cat, amount)
        lines.append(f"🗑️ Removed the {label} budget" if amount == 0 else f"✅ {label} budget: {money(amount)}/month")

    if not lines:
        return [("text", "I didn't catch what to change. Try _food budget 6000_ or _add a category for Pets_.")]
    if any(l.startswith("✅") for l in lines):
        lines.append("\nSee them anytime with /budget")
    return [("text", "\n".join(lines))]
