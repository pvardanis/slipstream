"""The bench executor member: run one vllm bench serve cell, price it, scrape its cache.

Holds the per-cell executor the bench-client image runs — the command builder and runner,
the config loaders, the cost post-processors, the prefix-cache scraper, and the leak check —
behind the ``slipstream-bench`` CLI. Depends inward on the ``contract`` kernel alone; never
on the report or orchestration members, and never on Prefect or the plotting stack
(ADR-0017), so the image installs a framework-free, plotting-free path.
"""
