# Getting started

A five-minute guide for first-time users. For the full feature list and options,
see [README.md](README.md).

## What this is

A dividend dashboard that runs on your own computer and opens in your web browser.
It looks up prices, yields, dividend history and ex-dividend dates for the ETFs,
funds and stocks you enter, using free Yahoo Finance data. There is no account,
no sign-up and no API key, and your holdings never leave your computer.

## 1. Install Python (one time)

You need Python 3.9 or newer. Nothing else needs installing.

- **Windows:** download it from [python.org/downloads](https://www.python.org/downloads/).
  On the first installer screen, tick **"Add python.exe to PATH"**, then click Install Now.
- **Mac:** download it from [python.org/downloads](https://www.python.org/downloads/) and run the installer.
- **Linux:** Python 3 is usually already installed.

To check, open a command prompt (Windows) or Terminal (Mac) and type
`python --version` (or `python3 --version` on a Mac). It should print 3.9 or higher.

## 2. Start the dashboard

Unzip the folder anywhere you like (for example, your Documents folder), then:

- **Windows:** double-click `run_windows.bat`.
- **Mac / Linux:** open Terminal in the folder and run `./run_mac_linux.sh`.
  If it says "permission denied", run `chmod +x run_mac_linux.sh stop_mac_linux.sh` once first.

A small command window opens and your browser shows the dashboard at
http://127.0.0.1:8765. Leave the command window open while you use it.

**Windows warning?** If Windows says "Windows protected your PC", click
**More info → Run anyway**. That appears for any downloaded script.

## 3. Put in your own holdings

The first time it runs, the dashboard fills in a few **sample** holdings
(SCHD, JEPI, VYM, O and BND) so you can see how it works. Replace them with yours:

1. Click **Edit holdings** at the top of the page.
2. Change or remove the sample rows and add your own: the ticker symbol, the number
   of shares, and your average cost per share. Leave shares blank to just watch a symbol.
3. Click **Save**.

You can also click **Import CSV** to load a file exported from your broker
(it understands columns like Symbol, Quantity and Cost basis), or edit
`holdings.csv` in Excel.

Your holdings are saved in `holdings.csv` in the same folder. That file is yours
alone: keep a copy somewhere safe, and don't send it along if you share the dashboard.

## 4. Find your way around

- **Top tiles:** portfolio value, yearly and monthly income, yield, and the next ex-dividend date.
- **Main table:** one row per holding. Click a column header to sort, click a row for full details.
- **Tabs:** Strategy & overlap, Holdings & performance, Monthly projections,
  Symbol comparison, and Research (look up any ticker, owned or not).
- **Calendar:** upcoming ex-dividend and pay dates. Click a day to see what's on it.
- **Flag:** tick it in Edit holdings to highlight a holding you're watching.
- **Auto / Light / Dark** in the header switches the theme.

Prices refresh every 15 minutes, or click **Refresh prices**.

## 5. Stop it

Click **Stop** at the top right of the page, or double-click `stop_windows.bat`
(Windows) / run `./stop_mac_linux.sh` (Mac/Linux), or press Ctrl+C in the command window.

## Common problems

| Problem | What to do |
|---|---|
| `python` is not recognized | Python isn't installed or wasn't added to PATH. Reinstall it and tick "Add python.exe to PATH". |
| Browser doesn't open | Open http://127.0.0.1:8765 yourself. |
| "Address already in use" | It's already running in another window. Stop it first, or start with `python server.py --port 9000`. |
| No prices / data shows as unavailable | Check your internet connection, then click Refresh prices. Yahoo occasionally blocks requests for a few minutes. |
| Want to look around without internet | Run `python server.py --demo` for made-up sample numbers. |

## Updating to a newer version

Unzip the new version over your existing folder. Your `holdings.csv` isn't in the
download, so your holdings are kept.

---

This is for information only, not investment advice. Data comes from Yahoo Finance
and can be delayed, estimated or wrong, so check anything important with your broker
or the fund's own website.
