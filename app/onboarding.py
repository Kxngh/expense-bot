"""First-contact flow: one welcome message, a YES to start, done.

Until someone says YES, nothing they send is stored as an expense or sent to an AI
provider - we only keep their number, profile name and first message (to process
after they agree).
"""
from . import config, db

YES = {"yes", "y", "ok", "okay", "haan", "ha", "han", "sure", "start", "agree", "👍", "yes!", "yess", "yup", "yeah"}
NO = {"no", "n", "nope", "nahi", "stop"}


def _ai_note() -> str:
    base = (f"🔒 *Privacy:* your expenses are stored on {config.BOT_OWNER_NAME}'s server. "
            "Other people using this bot can't see them.")
    what = "Messages and bill photos are" if config.PARSE_IMAGES else "Message text is"
    if config.LLM_PROVIDER == "gemini":
        return (f"{base} {what} sent to Google Gemini (free tier) to be understood; Google may "
                "review it and use it to improve its models."
                + ("" if config.PARSE_IMAGES else " Photos aren't sent."))
    if config.LLM_PROVIDER == "claude":
        return f"{base} {what} sent to Anthropic's Claude to be understood; Anthropic doesn't train on API data by default."
    return base


def welcome(name: str | None) -> str:
    hi = f"👋 Hi {name.split()[0]}!" if name else "👋 Hi!"
    return (
        f"{hi} I'm an expense tracker that lives in WhatsApp.\n\n"
        "Just text what you spend - *220 swiggy* or *chai 20, auto 80* - and I'll categorise it. "
        "Ask things like _how much on food this month?_ or set a budget in plain words: "
        "_my food budget is 6000_. You'll get a summary every Sunday and on the 1st.\n\n"
        f"{_ai_note()}\n"
        "You can wipe everything anytime with /deletemydata.\n\n"
        "Reply *YES* to start."
    )


DONE = ("✅ You're all set{name}!\n\n"
        "Try logging something, like *chai 20*.\n"
        "• Made a mistake? /undo\n"
        "• Want limits? _keep shopping under 3000_\n"
        "• Everything else: /help")


async def gate(wa_id: str, text: str | None, profile_name: str | None):
    """Returns (replies, text_to_process). replies=None means the user is active: handle normally."""
    user = db.get_user(wa_id)
    if profile_name and (not user or user["name"] != profile_name):
        db.update_user(wa_id, name=profile_name)
        user = db.get_user(wa_id)

    state = user["state"] if user else "new"
    if state == "active":
        return None, text

    norm = (text or "").strip().lower().strip(".!")
    if state == "new":
        # keep what they sent first (e.g. "220 swiggy") to handle right after they agree
        keep = text if text and norm not in YES | {"hi", "hello", "hey", "/start", "start"} else None
        db.update_user(wa_id, state="awaiting_consent", pending_text=keep)
        return [("text", welcome(profile_name or (user["name"] if user else None)))], None

    # awaiting_consent
    if norm in YES:
        pending = user["pending_text"]
        db.update_user(wa_id, state="active", consented_at=db.now().isoformat(), pending_text=None)
        name = f", {user['name'].split()[0]}" if user["name"] else ""
        replies = [("text", DONE.format(name=name))]
        return replies, pending  # pending (if any) is processed right after
    if norm in NO:
        db.delete_user_data(wa_id)
        return [("text", "No problem - nothing's been saved. Message me anytime if you change your mind.")], None
    if norm == "/deletemydata":
        db.delete_user_data(wa_id)
        return [("text", "Done - nothing of yours is stored.")], None
    return [("text", "Just reply *YES* to start (or *NO* if you'd rather not).")], None
