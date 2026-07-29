# Bank Bonuses

A small self-hosted tracker for bank/credit-union account signup bonuses
("churning"). Ships with a curated, sourced list of current US offers, and
lets one admin (you) track progress on each: start a bonus, check off its
requirements, mark it paid, and close the account once it's safe to — after
which it greys out until your cooldown period is up.

The bonus catalog is public and read-only for anyone with the link. Only the
admin (logged in with a password you set) can start/edit/close anything.

## Running it

```
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env   # edit .env and set your own BONUS_ADMIN_PASSWORD
./venv/bin/python app.py
```

Serves on `127.0.0.1:8935` by default (see the bottom of `app.py`). Put
whatever reverse proxy or tunnel you like in front of it.

Log in at `/bonus/login` with the password from `.env`. The session cookie
lasts a year, so you shouldn't need to log in again on the same browser.

## Making it yours

The entire bonus catalog lives in `banks.py` as a plain Python list — add,
remove, or edit entries there. Each bank needs:

- `hold_days` — how many days after starting it's safe to close the account
  (derived from the offer's early-closure terms)
- `cooldown_days` — how many days after closing before you're eligible for
  that bonus again (`None` for one-time-only offers)
- `checklist` — the short list of steps a user checks off

Your personal progress (which bonuses you've started, checked off, or closed)
lives in `data/state.json`, created automatically on first run and never
committed to git — it's just you.

You can also edit any bank's details straight from the page (as admin) via
the "Edit details" disclosure on its card — subtitle, requirement, fees,
exit terms, cooldown text, the balance-requirement badge, and the two
numbers that drive the date math (`hold_days`, `cooldown_days`). Those edits
are saved to `data/overrides.json` (also gitignored) layered on top of
`banks.py`, so pulling catalog updates from upstream won't clobber your
tweaks. Leave a field blank and save to revert it to the `banks.py` default.

## Data

The included offers were researched against Doctor of Credit and each bank's
own terms pages as of the "verified" date on the page. Bank bonus terms
change constantly — always confirm current terms before opening an account
based on this list.
