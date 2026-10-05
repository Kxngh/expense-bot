"""Expense categories and a keyword fallback used when the LLM is unavailable."""

CATEGORIES = [
    "Food & Dining",
    "Groceries",
    "Transport",
    "Shopping",
    "Bills & Utilities",
    "Rent",
    "Entertainment",
    "Health",
    "Education",
    "Travel",
    "Subscriptions",
    "Personal Care",
    "Gifts & Donations",
    "Investments",
    "Other",
]

# Big once-a-month costs, kept out of daily-pace projections
FIXED = {"Rent", "Investments"}

# Short aliases you can type in commands, e.g. "/budget food 6000"
ALIASES = {
    "food": "Food & Dining", "dining": "Food & Dining", "eating": "Food & Dining",
    "grocery": "Groceries", "groceries": "Groceries",
    "transport": "Transport", "travel": "Travel", "commute": "Transport",
    "shopping": "Shopping", "bills": "Bills & Utilities", "utilities": "Bills & Utilities",
    "rent": "Rent", "fun": "Entertainment", "entertainment": "Entertainment",
    "health": "Health", "medical": "Health", "education": "Education",
    "subs": "Subscriptions", "subscriptions": "Subscriptions",
    "personal": "Personal Care", "care": "Personal Care",
    "gifts": "Gifts & Donations", "donations": "Gifts & Donations",
    "investments": "Investments", "invest": "Investments", "other": "Other",
}

# Keyword -> category, for the offline fallback parser
KEYWORDS = {
    "Food & Dining": ["swiggy", "zomato", "lunch", "dinner", "breakfast", "chai", "tea",
                      "coffee", "cafe", "restaurant", "pizza", "burger", "snack", "food", "dominos"],
    "Groceries": ["blinkit", "zepto", "bigbasket", "instamart", "grocery", "groceries",
                  "vegetables", "milk", "fruits", "dmart"],
    "Transport": ["uber", "ola", "rapido", "auto", "metro", "cab", "petrol", "diesel",
                  "fuel", "parking", "toll", "bus", "fastag"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "clothes", "shoes", "shopping"],
    "Bills & Utilities": ["electricity", "bill", "recharge", "wifi", "broadband",
                          "internet", "gas", "water", "jio", "airtel"],
    "Rent": ["rent", "pg"],
    "Entertainment": ["movie", "pvr", "inox", "concert", "game", "bookmyshow", "party"],
    "Health": ["medicine", "pharmacy", "doctor", "hospital", "gym", "apollo", "1mg"],
    "Education": ["course", "book", "udemy", "tuition", "exam"],
    "Travel": ["flight", "train", "irctc", "hotel", "trip", "airbnb", "makemytrip"],
    "Subscriptions": ["netflix", "spotify", "prime", "hotstar", "youtube", "subscription",
                      "chatgpt", "claude", "icloud"],
    "Personal Care": ["haircut", "salon", "grooming", "laundry"],
    "Gifts & Donations": ["gift", "donation", "charity"],
    "Investments": ["sip", "mutual fund", "stocks", "fd", "zerodha", "groww"],
}


MAX_CUSTOM = 15


def for_user(wa_id: str | None) -> list[str]:
    """Built-in categories plus this person's own ones (never anyone else's), 'Other' last."""
    if not wa_id:
        return list(CATEGORIES)
    from . import db
    return CATEGORIES[:-1] + db.custom_categories(wa_id) + ["Other"]


def clean_name(name: str) -> str:
    name = " ".join(str(name).replace("*", "").replace("_", " ").split())[:30]
    return name[:1].upper() + name[1:] if name else ""


def resolve(name: str, wa_id: str | None = None) -> str | None:
    """Map user input like 'food' or 'Food & Dining' to one of this user's categories."""
    n = name.strip().lower()
    if not n:
        return None
    if n in ALIASES:
        return ALIASES[n]
    cats = for_user(wa_id)
    for c in cats:
        if c.lower() == n:
            return c
    for c in cats:
        if c.lower().startswith(n):
            return c
    return None


def guess(text: str) -> str:
    t = text.lower()
    for cat, words in KEYWORDS.items():
        if any(w in t for w in words):
            return cat
    return "Other"
