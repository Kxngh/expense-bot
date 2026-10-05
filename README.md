# WhatsApp Expense Bot

Text your expenses to a WhatsApp number. The bot logs them, categorizes them, tracks budgets, and sends trend charts back into the chat.

```
You:  chai 20, auto 80, dinner 450
Bot:  ✅ ₹20 · Food & Dining · Chai  #41
      ✅ ₹80 · Transport · Auto  #42
      ✅ ₹450 · Food & Dining · Dinner  #43
      Total ₹550

      ⚠️ Food & Dining at 84% of budget (₹640 left)
      Total: ₹18,240 / ₹40,000 this month
```

**What it handles**
- Free text, including several items at once: `220 swiggy`, `1.2k shoes yesterday`, `rent 12000`
- Photos of bills and UPI screenshots, read by the AI (when `PARSE_IMAGES` is on)
- Questions in plain language, answered directly:
  - "how much on swiggy this month?" → *₹3,240 on Swiggy this month (1–20 Oct)*, 9 expenses, avg ₹360
  - "how many times did I take an auto last week?" → a count plus the total
  - "biggest expenses in september", "average daily spend on food", "show my uber rides"
  - "food this month vs last month", "where does my money go?" (category breakdown with a chart)

  The AI only turns the question into a filter: dates, category, keyword, and what to calculate. The bot runs that filter on the asking user's own rows. The AI never writes SQL, so a strange question can't change or expose anyone's data.
- Budget alerts at 80% and 100%, per category and for the whole month
- Charts: category breakdown (`/month`), 6-month trend (`/trends`), and spending pace vs. budget (`/daily`)

## Onboarding

The first message from a new number gets one welcome message. It explains what the bot does, where data is stored, and which AI reads the messages (Gemini free tier or Claude, with the matching privacy note), then asks the person to reply **YES**.
- Before they say YES, nothing they send is saved as an expense or sent to an AI.
- If their first message was an expense (`220 swiggy`), it's kept and logged right after they agree.
- **NO** erases even the phone number.
- People who were already logging before you upgraded skip the welcome.

Everything else is set up in plain words, whenever they like:
- *"my food budget is 6000"*, *"I want to spend 30k a month"*, *"drop the shopping budget"*
- *"add a category for Pets"*, *"add Gym with a 1500 budget"*, *"remove the Pets category"* (its expenses move to Other)

Built-in categories can't be removed, and each person can have up to 15 custom ones.

`/deletemydata` permanently erases that person's expenses, budgets, categories and reports after they confirm with **DELETE**.

## Privacy and isolation between users

- Every database query is scoped to the phone number that sent the message. A person can never see, search, change or delete someone else's entries, even by guessing an entry `#id`.
- Custom categories belong to one person. The AI only ever receives the sender's own message and their own category names, never anyone's stored expenses.
- Reports are built per person.
- Webhooks must carry Meta's signature (`APP_SECRET`). Without it the server rejects every message. Otherwise someone who found the URL could send fake messages "from" another person's number.
- `tests/test_isolation.py` gives two users unique data, then runs every command, every kind of question, reports, exports, settings, and attempts to touch the other person's entries by `#id`. It checks that nothing crosses over.
- **What isolation doesn't cover:** you, as the server owner, can read the database file. The welcome message says data is stored on your server, not that nobody can see it. Keep the VM's SSH key safe, and don't share the `expenses.db` file.

## Automatic reports

**Weekly report** (Sunday 8 PM) and **monthly report** (1st of the month, 9 AM), for each person:
- **What was spent:** total, day-to-day spend per day, and comparison with last week or month and with your recent average. Rent and investments are shown separately so they don't distort the comparison.
- **On what:** every category with its share and how it changed, plus a chart.
- **How:** what you spend on most often ("Auto ×11"), where the most money went, the biggest single expense, weekend vs weekday spending, and no-spend days.
- **Budgets:** the weekly report shows where the month stands and warns if your pace will take you over. The monthly report gives a ✅/🚨 verdict per budget, plus a 6-month trend chart.

**About WhatsApp's 24-hour rule.** A bot can send free-form messages only within 24 hours of the person's last message to it.
- If the person messaged in the last 24 hours, the report is sent right away.
- If not, it's held and delivered just before the reply to their next message, at no cost.
- If you'd rather nudge them right away, create a utility template in WhatsApp Manager (e.g. "Your weekly spending report is ready - reply anything to see it") and set `REPORT_TEMPLATE`. Meta charges a small fee per template message.

Reports are never sent twice, even after a restart. If the server was down at report time, it catches up within 3 days. Weeks or months with no expenses are skipped. Change the times in `.env`.

## Commands

| | |
|---|---|
| `/today` `/week` `/month` | Summaries (`/month` includes a chart) |
| `/month 2026-09` | A past month |
| `/cat food` | Every entry in one category this month |
| `/trends` | Monthly totals chart, plus the biggest category changes vs. the same point last month |
| `/daily` | Cumulative spend vs. an even pace to your budget (rent is treated as paid up front) |
| `/budget` | All budgets and usage |
| `/budget total 40000` · `/budget food 6k` · `/budget food 0` | Set or remove a budget |
| `/recent` | Last 10 entries with their `#ids` |
| `/undo` · `/delete 12` · `/fix 12 groceries` | Correct mistakes |
| `/export` | CSV of the month, sent as a file |
| `/report week` · `/report month` | The full report for this week or month so far |
| `/report lastweek` · `/report lastmonth` | The last complete week or month |
| `/reports off` · `/reports on` | Stop or restart automatic reports |
| `/categories` | Built-in and your own categories |
| `/deletemydata` | Erase everything (asks for confirmation) |

## Setup (about 20 minutes)

### 1. Create the Meta app
1. Go to <https://developers.facebook.com/apps>, click **Create app**, and choose a **Business** type app.
2. Add the **WhatsApp** product. Meta creates a test WhatsApp Business account with a free **test phone number**.

### 2. Allow your personal number
On **WhatsApp → API Setup**:
- Copy the **Phone number ID** into `PHONE_NUMBER_ID`. This is the ID, not the number itself.
- Under **To**, add your personal WhatsApp number and verify it with the OTP.
- Save the test number in your phone's contacts (as "Expenses", for example).

### 3. Get a token that doesn't expire
The token shown on API Setup only lasts 24 hours. To get a permanent one:
1. Open **Business Settings → Users → System users** and add a system user with the Admin role.
2. Click **Add assets** and give it full control of your app and your WhatsApp account.
3. Click **Generate token**, select your app, and tick `whatsapp_business_messaging` and `whatsapp_business_management`. Set expiry to **Never**.
4. Put the token in `WHATSAPP_TOKEN`.

Then copy **App settings → Basic → App secret** into `APP_SECRET`. This is required: the bot rejects unsigned webhooks.

### 4. Run the server
```bash
cp .env.example .env          # fill in the values
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```
Meta needs a public HTTPS URL. For local testing:
```bash
cloudflared tunnel --url http://localhost:8000     # or: ngrok http 8000
```

### 5. Connect the webhook
On **WhatsApp → Configuration → Webhook**:
- **Callback URL**: `https://<your-tunnel-or-domain>/webhook`
- **Verify token**: the same value as `VERIFY_TOKEN`
- Click **Verify and save**, then **Manage** and subscribe to the **messages** field.

### 6. Say hi
Send `/help` to the test number from your phone.

## Free hosting (₹0)

| Host | Card needed | Replies when idle | Fit |
|---|---|---|---|
| **Vercel + Neon** (recommended) | No | A few seconds (serverless cold start) | Best free option |
| Render + Neon | No | About 1 minute (wakes after 15 min of sleep) | Works, just slower |
| Oracle Cloud Always Free VM | Yes | Instant, always on | Good if you get a VM |
| Google Cloud Run + Neon | Yes (billing account) | A few seconds | Generous free tier |

Koyeb, Fly.io and Railway no longer have a usable free tier for this: they want a card hold, a trial, or a tiny monthly credit.

All of them use **Neon** for the database (free, no card): 1 GB of storage and 100 compute-hours a month. The app keeps Neon asleep when nobody is using it.
- Database connections close after a minute idle.
- The report scheduler doesn't poll the database every minute.

### Step 1. Neon (database)
1. Sign up at <https://neon.tech> and create a project in **AWS Asia Pacific (Singapore)**.
2. Copy the **pooled** connection string, whose hostname contains `-pooler`. It goes in `DATABASE_URL`.

The tables are created automatically on first use.

### Step 2a. Vercel (recommended)
Vercel runs the app as serverless functions. Each webhook starts the app if needed (a few seconds), handles the message, and replies. The project already includes `index.py` and `vercel.json`, which runs the functions in Singapore next to Neon.

1. Push this folder to a **private** GitHub repo.
2. Sign up at <https://vercel.com> with GitHub. The **Hobby** plan is free, needs no card, and is meant for personal projects like this.
3. Click **Add New → Project** and import the repo. Vercel detects the Python app on its own.
4. Under **Environment Variables**, add:
   - `DATABASE_URL`
   - `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, `VERIFY_TOKEN`, `APP_SECRET`
   - `ALLOWED_NUMBERS`, `GEMINI_API_KEY`, `LLM_PROVIDER=gemini`
   - `BOT_OWNER_NAME`, `CRON_SECRET`
5. Click **Deploy**. Your webhook URL is `https://<project>.vercel.app/webhook`. Put it in Meta's webhook settings (setup step 5).

Vercel sets `VERCEL=1` automatically, and the app switches to serverless mode:
- It handles each message before responding, because a function can be frozen right after it responds.
- It sets up the database on first use.
- It leaves reports to the cron jobs in Step 3.

If Meta's webhook check fails with a 401 from Vercel, open **Project → Settings → Deployment Protection** and make sure the production URL isn't protected.

### Step 2b. Render (alternative)
1. Push to a private GitHub repo.
2. On Render, click **New → Blueprint** and pick the repo. `render.yaml` sets up a free web service.
3. Fill in the same environment variables.

The webhook URL is `https://<service-name>.onrender.com/webhook`. The app sleeps after 15 idle minutes, so the first reply after a quiet spell takes about a minute. Meta retries webhooks, so no messages are lost.

### Step 3. Reports on schedule (cron-job.org, free)
On serverless or sleeping hosts nothing runs between requests, so a free external cron triggers the reports:

1. Pick a long random `CRON_SECRET` and add it to your host's environment variables.
2. At <https://cron-job.org>, add two jobs that call `https://<your-app>/tasks/reports?key=<CRON_SECRET>`:

| Job | Schedule (time zone Asia/Kolkata) | UTC equivalent |
|---|---|---|
| Weekly report | Sundays at **20:01** and **20:06** | Sundays 14:31 and 14:36 |
| Monthly report | 1st of the month at **09:01** and **09:06** | 1st at 03:31 and 03:36 |

The second run is a safety net in case the first one times out while the app starts. Each report is only ever sent once, however often the URL is called.

Reports reach everyone who messaged the bot in the last 24 hours (WhatsApp's rule). Everyone else gets theirs with their next message.

On an always-on server (Oracle, or `uvicorn` on your own machine), the app checks for due reports every minute by itself, so the cron is optional there.

### Oracle Cloud VM (alternative)
Oracle's VM is always on with a persistent disk, so SQLite works and you don't need Neon:
```bash
docker build -t expense-bot .
docker run -d --env-file .env -v $(pwd)/data:/data -p 8000:8000 expense-bot
```

## Costs
- **WhatsApp**: replies to messages you send are free. The bot never messages you first, so it stays in the free tier.
- **AI parsing**: choose one with `LLM_PROVIDER` in `.env`.
  - `gemini` (free): uses a key from [AI Studio](https://aistudio.google.com/apikey). Free-tier limits are far above what 2–3 people need. Photos are off by default, but a caption on a photo (`1834 dmart`) still gets logged, and the photo itself is never sent anywhere. **Privacy:** on the free tier, Google may use messages to improve its models and human reviewers may read them. Make sure everyone using the bot is OK with that. Turning on billing for the key moves it to paid-tier terms.
  - `claude` (paid): Haiku costs roughly ₹0.1–0.2 per logged text and ₹0.3–0.5 per receipt photo. Expect about ₹40–50/month per active user.
  - `none`: a keyword parser that handles `220 swiggy` style messages but can't answer questions.
  - Slash commands never call an AI. If the AI call fails or is rate-limited, the keyword parser takes over, so expenses still get logged.
  - Check in AI Studio that `GEMINI_MODEL` is on the free tier for your key. Google renames models often; any Flash or Flash-Lite model works.

## How it works
```
WhatsApp ──webhook──▶ /webhook (FastAPI)
                        ├─ verify signature, allow-list, dedupe retries
                        └─ background task
                             ├─ /command? ─▶ commands.py ─▶ text / chart PNG / CSV
                             └─ else ─▶ parser.py (Gemini/Claude → JSON)
                                         ├─ log   → db.py (SQLite) + budget alerts
                                         ├─ query → query.py (validated filter → answer)
                                         └─ budget/trend question → built-in command
```
| File | Role |
|---|---|
| `app/main.py` | Webhook endpoints, signature check, dedupe |
| `app/handler.py` | Message → replies, budget alerts |
| `app/parser.py` | Claude or Gemini parsing (same JSON schema) plus the offline fallback |
| `app/commands.py` | Slash commands and summaries |
| `app/query.py` | Answers questions from a validated filter, scoped to the asking user |
| `app/reports.py` | Weekly and monthly reports, scheduling, and the 24h-window delivery rules |
| `app/onboarding.py` | Welcome, YES-to-start consent, first-message handling |
| `app/settings.py` | Budgets and custom categories from plain language, validated per user |
| `app/charts.py` | matplotlib charts |
| `app/db.py` | Storage: SQLite file, or Postgres/Neon when `DATABASE_URL` is set |
| `app/categories.py` | Categories, aliases, and keywords. Edit these to fit your spending. |

Run the tests with `pytest -q`. They mock WhatsApp and the AI. To run the same suite against Postgres, use `TEST_DATABASE_URL=postgresql://... pytest -q`.

## Ideas for later
- Recurring expenses (rent, subscriptions) auto-logged on a set day.
- Split expenses with friends.
