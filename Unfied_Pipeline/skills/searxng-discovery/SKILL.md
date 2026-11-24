---
name: searxng-discovery
description: Query patterns and pagination rules for finding NSE delisting sources through the search providers (local SearXNG, then Serper.dev / Brave API when configured).
---

# SearXNG Discovery

## Command
```bash
.venv/bin/python scripts/web_search.py --query "<query>" --pages 2 --year-tag <2007|2017|2020|2024|master|YYYY>
```
- Output: one JSON summary line with `cache_files`, `result_count`, `new_urls`, and the top URLs.
- Results are cached in `data/search_cache/`. Re-running the same query+page is free (no network).
- `--pages N` walks results pages 1..N (`providers` in the output says which provider answered). Use 1–3 pages. Stop paging when a page adds 0 new URLs.
- `--list-cache` prints the cached queries so you don't repeat them.

## Query templates
Replace `{Y}` with the year. Any website can be a source: NSE rate-limits hard (a few requests per run),
so finding the same information elsewhere is valuable.

**Open web (finds new websites — no `site:`)**
- `list of companies delisted from NSE {Y}`
- `"<company name>" delisted NSE` / `"<company name>" delisting exit price`
- `compulsorily delisted companies {Y} list`
- `delisting offer {Y} reverse book building result`

**New websites** — for each site in `indicators.new_domains_with_rows` (`data/metadata/domain_registry.json`):
- `site:<domain> delisted companies`, `site:<domain> delisting {Y}`

**Primary (official)**
- `site:nseindia.com delisting circular {Y}`
- `site:nseindia.com compulsory delisting {Y}`
- `site:nseindia.com "delisted" companies list`
- `site:nseindia.com voluntary delisting exit offer {Y}`
- `site:sebi.gov.in delisting order {Y}`
- `site:bseindia.com compulsory delisting notice {Y}`

**Category-specific**
- Compulsory: `NSE compulsory delisting order {Y} suspended companies`
- Voluntary: `NSE voluntary delisting reverse book building {Y} exit price`
- IBC/NCLT: `NCLT resolution plan delisting NSE {Y}`, `site:ibbi.gov.in delisting {Y}`
- Merger: `amalgamation scheme delisted from NSE {Y}`

**Year-specific hooks**
- 2007: `SEBI delisting guidelines 2003 NSE {Y}`, `NSE delisted securities 2007`
- 2017: `NSE compulsory delisting 2017 shell companies`, `exchanges delist 200 companies 2017`
- 2020: `delisting COVID 2020 NSE SEBI relaxation`, `IBC delisting 2020 NSE`
- 2024: `SEBI delisting amendment 2024 fixed price NSE`, `NSE delisting 2024 counter offer`

**Master lists**: `site:nseindia.com list of delisted companies xls`, `NSE delisted companies archive` (use `--year-tag master`).

## Rules
- One topic per query. Short queries (≤ 8 words) work best.
- Don't paste search results into chat; read the summary and pass the cache file to `polite_fetcher.py`.
- If SearXNG returns 0 results for a site: query, retry once without `site:` plus the domain name as a keyword.
- Prefer open-web queries over more `site:nseindia.com` ones: the NSE fetch budget is small.
- If the client reports `"mode": "html_fallback"`, JSON is disabled in SearXNG — tell the user.
