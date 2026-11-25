# Hosting a live demo on Render

You can put a public, read-only demo of the dashboard on a free [Render](https://render.com)
account, so people can try it without installing anything.

The demo:

- Shows the sample portfolio in [`demo/demo-holdings.csv`](demo/demo-holdings.csv) (26 well-known
  dividend ETFs and stocks, with Core focus, flags, notes and two watchlist symbols) and the two
  saved Planning scenarios in [`demo/demo-scenarios.json`](demo/demo-scenarios.json).
  **Your own `holdings.csv` is never used or uploaded**: it is ignored by git, so it isn't in the repository.
- Is read-only. Edit holdings, Import CSV and Stop are hidden, Planning can't save scenarios, and the
  server refuses every change request. Visitors can still filter, sort, export, use Research and
  Symbol comparison, and try what-if plans (those stay in their own browser).
- Uses live Yahoo Finance prices, cached for 6 hours. The server loads the whole sample portfolio
  when it starts and again before the cache runs out, so visitors get cached data instead of each
  page view going to Yahoo. The Refresh button reloads from that cache. Visitors looking up new
  symbols (Research, Symbol comparison, Planning) are limited to 40 new symbols per 10 minutes in total.
- Falls back to sample numbers for a holding if Yahoo can't be reached, so the page never comes up empty.

## Deploy it

1. Sign in to [dashboard.render.com](https://dashboard.render.com) (your free account).
2. Click **New** > **Blueprint**.
3. Connect your GitHub account if asked, and pick the `dividend-dashboard` repository.
4. Render finds `render.yaml` and shows one web service, **dividend-dashboard-demo**, on the
   Free plan. Click **Apply** (or **Deploy Blueprint**).
5. Wait for the first deploy to finish (a minute or two). The service page shows the address,
   like `https://dividend-dashboard-demo.onrender.com`. That's the link to share.

If you'd rather not use a Blueprint: **New** > **Web Service**, pick the repository, then set
Runtime **Python 3**, Build command `pip install -r requirements.txt`, Start command
`python server.py --public-demo`, Instance type **Free**, and add the environment variable
`PYTHON_VERSION` = `3.12.8`.

Every push to the main branch redeploys it automatically. To change the sample portfolio, edit
the two files in `demo/` and push.

## Fund details snapshot

Yahoo often refuses fund details (expense ratio, category, top holdings, sectors, company
description) to hosting services like Render, while still sending prices and dividends. Then every
holding shows "Some fund details missing" and the overlap panels are empty.

The fix is to save those details once on your own computer, where Yahoo answers, and ship them with
the demo. Fund details change slowly, so doing this every few months is plenty.

1. On your computer, in the dashboard folder (with this version of the code), run:

   ```
   python server.py --save-demo-snapshot
   ```

   It looks up the demo portfolio plus SPY and QQQ and writes `demo/fund-snapshot.json`.
   It doesn't touch your holdings.
2. Commit and push that file (or send it to whoever maintains the repo). Render redeploys by itself.

The demo still uses live prices and dividends. It only falls back to the snapshot when Yahoo holds
back the fund details. Symbols visitors look up that aren't in the snapshot keep the warning.

## Settings

Environment variables you can change on the service's **Environment** page:

| variable | default | what it does |
|---|---|---|
| `DEMO_CACHE_HOURS` | `6` | How long live prices are reused before fetching them again |
| `DEMO_DATA` | `live` | `synthetic` shows made-up sample numbers and never contacts Yahoo |

## Good to know

- **First visit can be slow.** A free Render service sleeps after 15 minutes without visitors.
  The next visit wakes it, which takes about 30 to 60 seconds, and then loads prices for the
  sample portfolio (a few more seconds). After that it is fast until it sleeps again.
- **If Yahoo blocks Render's servers**, holdings show a "showing sample numbers" note and names
  starting with "Demo". The **Logs** page on Render then says
  `symbols using sample numbers: Yahoo unreachable`. If that keeps happening, set
  `DEMO_DATA` to `synthetic` so the whole demo is consistent sample data.
- **Try it on your computer first:** `python server.py --public-demo` and open
  http://127.0.0.1:8765. It uses only the demo files, never your holdings.
