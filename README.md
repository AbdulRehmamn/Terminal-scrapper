# LeadScraper

Pulls business leads (company, type, trades, phone, email, website, address, social links) from:

- **Procore Network**: a whole state, or any Procore listing URL
- **Any directory site** that has listing pages linking to company pages
- **Company websites**: homepage plus contact, about and team pages
- **PDFs**: plan-holder lists, bid lists, member directories (local files or links)

It also **enriches** a leads file by visiting each company's website to find emails.

Every lead gets a **Lead Score** (0–100) and a **Signals** column. Signals are buying signals found on the company's pages, such as "now hiring", "estimator", "bidding" or "request a quote". Use them to decide who to contact first.

## Quick start (Windows)

Double-click **start.bat**. On the first run it does the setup for you:

- It finds Python, or installs it through winget if it's missing.
- It creates a private environment in a `.venv` folder.
- It installs every required package.

Then it shows a menu: pick a number and it runs. Later runs skip the setup and go straight to the menu.

## Manual setup (Mac/Linux, or if you prefer)

1. Install Python 3.9 or newer from python.org.
2. Open a terminal in this folder and run:

```
pip install -r requirements.txt
```

## Get Procore leads (any number)

```
python leadscraper.py procore --state ca --max 1000     # stop after 1,000 leads
python leadscraper.py procore --state ca                # no limit: every company in the state
```

Results go to `procore_leads.csv`, which opens in Excel or Google Sheets. At the default pace (about 3 seconds per lead) 1,000 leads take about an hour; the whole of California (about 47,000 companies) takes a couple of days.

**Stop and resume any time.** Press Ctrl+C, then run the same command again. It picks up where it left off and skips leads it already has.

### Useful filters

```
# only concrete and electrical companies
python leadscraper.py procore --state ca --trade "concrete,electrical"

# only San Diego and Los Angeles, and only leads with a phone and a website
python leadscraper.py procore --state ca --city "san diego,los angeles" --require phone,website

# a different state
python leadscraper.py procore --state tx --out texas_leads.csv
```

### Then find emails

Procore doesn't show emails. For the leads that have a website, this visits the site and finds them:

```
python leadscraper.py enrich procore_leads.csv
```

This writes `procore_leads_enriched.csv`.

## Other sources

```
# Company websites: one per line in domains.txt, or listed directly
python leadscraper.py site --file domains.txt
python leadscraper.py site acmeconcrete.com jemiron.com

# PDFs: local files or links
python leadscraper.py pdf planholders.pdf https://example.org/bidders-list.pdf

# Any directory: {page} marks the page number; --follow matches company links
python leadscraper.py directory --list-url "https://example.com/contractors?page={page}" --follow "/company/" --end-page 40
```

To find the `--follow` pattern, open a company page from the directory in your browser and look at its address. For example, `https://site.com/company/acme-123` means `--follow "/company/"`.

## Being a good guest (and not getting blocked)

- The scraper obeys each site's `robots.txt` and waits between requests (`--delay`, default 3 seconds).
- If a site shows a CAPTCHA or a block page, the scraper **stops and saves progress** rather than hammering the site. Wait an hour or two, then rerun with a slower pace, such as `--delay 6`.
- The scraper does not log in, solve CAPTCHAs or hide what it is. Sites that need an account, such as most of The Blue Book, can't be scraped this way. Use their export or subscription instead.
- Check each site's terms of use before scraping. When you email the leads, follow anti-spam rules (CAN-SPAM in the US): identify yourself, include your address and offer an opt-out.

## Columns

Company, Company Type, Trades, Phone, Email, Website, Address, City, State, Zip, LinkedIn, Facebook, Instagram, X/Twitter, YouTube, Lead Score, Signals, Source URL, Scraped At
