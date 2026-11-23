#!/usr/bin/env python3
"""Free-tier research runner: Tavily evidence + Groq or OpenRouter extraction.

Select a provider explicitly with --provider. Both modes use free endpoints;
this script does not enable billing or fall back to a paid provider.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
APPS = json.loads((ROOT / "data/apps.json").read_text())
SCHEMA = json.loads((ROOT / "agent/schema.json").read_text())
OUT = ROOT / "data" / "live_research"


def canonical_url(url):
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def clean_schema(node):
    if isinstance(node, dict):
        return {
            k: clean_schema(v)
            for k, v in node.items()
            if k not in ("maxLength", "minLength", "minItems", "maxItems", "$comment")
        }
    if isinstance(node, list):
        return [clean_schema(x) for x in node]
    return node


def search_sources(session, key, app):
    queries = [
        f"{app['name']} official developer API authentication documentation",
        f"{app['name']} official API pricing free developer account access",
        f"{app['name']} official MCP server documentation",
    ]
    found = {}
    for query in queries:
        response = session.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
                "include_raw_content": "markdown",
            },
            timeout=45,
        )
        response.raise_for_status()
        # One or two results per focused query are enough for this first pass;
        # sending every full page caused 413 request-size errors at the LLM.
        added_for_query = 0
        for item in response.json().get("results", []):
            url = item.get("url", "")
            if not url or url in found:
                continue
            found[url] = {
                "title": item.get("title", ""),
                "url": url,
                "text": (item.get("raw_content") or item.get("content") or "")[:1400],
            }
            added_for_query += 1
            if added_for_query >= 2:
                break
    return list(found.values())


def research_one(session, app, tavily_key, llm_key, model, endpoint, provider_label, provider):
    sources = search_sources(session, tavily_key, app)
    if not sources:
        raise RuntimeError("Tavily returned no source pages; no row created")

    schema = clean_schema(SCHEMA)
    # Strict JSON Schema requires every declared object property to be
    # required. Use an empty string when an evidence note is unnecessary.
    evidence = schema["properties"]["evidence"]["items"]
    evidence["required"] = ["claim", "url", "note"]

    user_payload = {
        "app": {k: app[k] for k in ("name", "hint", "category")},
        "as_of": "2026-11-23",
        "source_pages": sources,
        "task": (
            "Assess third-party API buildability from the supplied source pages. "
            "Prefer official vendor docs and pricing pages; never infer from memory. "
            "Return 2-6 evidence entries. "
            "Cite only URLs present in source_pages. If evidence is missing, use unknown "
            "or low confidence. Distinguish account signup from API credentials. "
            "free_self_serve means a working API credential can be obtained without "
            "human approval on a free plan, trial, or dev sandbox. official_mcp is true "
            "only for a vendor-shipped or vendor-announced MCP. Do not treat community "
            "MCP as official. Return exactly one JSON object matching the schema."
        ),
        "schema": schema,
    }
    request_body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are an evidence-first SaaS API researcher. Use only the supplied source pages."},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "app_research",
                    "strict": True,
                    "schema": schema,
                },
            },
            "temperature": 0.1,
            "max_completion_tokens": 5000,
        }
    if provider == "openrouter":
        # Avoid routes that silently ignore the schema constraint.
        request_body["provider"] = {"require_parameters": True}
    response = None
    for attempt in range(5):
        response = session.post(
            endpoint,
            headers={"Authorization": f"Bearer {llm_key}"},
            json=request_body,
            timeout=90,
        )
        if response.status_code != 429:
            response.raise_for_status()
            break

        body = response.text
        lower = body.lower()
        if any(term in lower for term in ("requests per day", "tokens per day", "daily limit", "per-day limit", "daily quota")):
            raise RuntimeError(f"{provider_label} free daily quota is exhausted. " + body[:500])
        wait_seconds = None
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                wait_seconds = float(retry_after)
            except ValueError:
                pass
        if wait_seconds is None:
            match = re.search(r"try again in\s+([0-9.]+)\s*s", body, re.I)
            wait_seconds = float(match.group(1)) if match else min(15 * (2 ** attempt), 120)
        if attempt == 4:
            raise RuntimeError(f"{provider_label} remained rate-limited after five attempts. " + body[:500])
        wait_seconds = max(1, min(wait_seconds, 180))
        print(f"RATE LIMIT {app['name']}: retrying in {wait_seconds:.0f}s ({attempt + 2}/5)", flush=True)
        time.sleep(wait_seconds + 1)

    row = json.loads(response.json()["choices"][0]["message"]["content"])
    row.update({k: app[k] for k in ("id", "name", "category", "hint")})
    row["research_provider"] = f"Tavily search + {provider_label}"
    allowed = {canonical_url(x["url"]): x["url"] for x in sources}
    verified_evidence = []
    for item in row.get("evidence", []):
        key = canonical_url(item.get("url", ""))
        if key in allowed:
            item["url"] = allowed[key]
            verified_evidence.append(item)
    row["evidence"] = verified_evidence
    if len(row["evidence"]) < 2:
        raise RuntimeError("Fewer than two cited URLs matched retrieved source pages; row withheld")
    (OUT / f"{app['id']:03}.json").write_text(json.dumps(row, indent=2, ensure_ascii=False))
    return row


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--only", help="Comma-separated app IDs, e.g. 1,2")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--provider", choices=("groq", "openrouter"), default="groq",
                   help="Free inference provider; existing rows are preserved")
    p.add_argument("--model", help="Override the selected provider's default model")
    p.add_argument("--no-resume", action="store_true")
    args = p.parse_args()
    tavily_key = os.environ.get("TAVILY_API_KEY")
    configs = {
        "groq": {
            "env": "GROQ_API_KEY",
            "endpoint": "https://api.groq.com/openai/v1/chat/completions",
            "label": "Groq free API tier",
            "model": "openai/gpt-oss-20b",
        },
        "openrouter": {
            "env": "OPENROUTER_API_KEY",
            "endpoint": "https://openrouter.ai/api/v1/chat/completions",
            "label": "OpenRouter free-model API",
            "model": "openrouter/free",
        },
    }
    config = configs[args.provider]
    llm_key = os.environ.get(config["env"])
    if not tavily_key or not llm_key:
        sys.exit(f"Set TAVILY_API_KEY and {config['env']} in this terminal. Do not add billing.")
    model = args.model or config["model"]
    OUT.mkdir(parents=True, exist_ok=True)
    # Bootstrap per-app checkpoints from the submitted aggregate if a user resumes
    # from the source package rather than the original local working directory.
    aggregate = ROOT / "data" / "live_raw.json"
    if aggregate.exists() and not any(OUT.glob("*.json")):
        try:
            for saved_row in json.loads(aggregate.read_text()):
                saved_id = int(saved_row["id"])
                (OUT / f"{saved_id:03}.json").write_text(
                    json.dumps(saved_row, indent=2, ensure_ascii=False)
                )
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            sys.exit(f"Could not bootstrap checkpoints from {aggregate}: {exc}")
    chosen = APPS if not args.only else [a for a in APPS if a["id"] in {int(x) for x in args.only.split(",")}]
    if not args.no_resume:
        chosen = [a for a in chosen if not (OUT / f"{a['id']:03}.json").exists()]

    errors = []
    # Sequential calls respect free-tier limits; failed rows remain resumable.
    with requests.Session() as session:
        for app in chosen:
            try:
                row = research_one(
                    session, app, tavily_key, llm_key, model, config["endpoint"],
                    config["label"], args.provider
                )
                print(f"OK {app['id']:03} {app['name']}: {row['buildability']}", flush=True)
            except Exception as exc:
                errors.append({"id": app["id"], "name": app["name"], "error": str(exc)})
                print(f"FAIL {app['name']}: {exc}", file=sys.stderr, flush=True)
                if any(term in str(exc).lower() for term in ("daily quota", "remained rate-limited")):
                    print("Stopping run. Saved checkpoints remain; rerun later to resume.", flush=True)
                    break

    rows = sorted((json.loads(f.read_text()) for f in OUT.glob("*.json")), key=lambda r: r["id"])
    (ROOT / "data" / "live_raw.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    present = {r["id"] for r in rows}
    (ROOT / "data" / "live_coverage.json").write_text(json.dumps({
        "expected": len(APPS),
        "researched": len(rows),
        "missing": [a["name"] for a in APPS if a["id"] not in present],
        "errors": errors,
        "providers": sorted({r.get("research_provider", "unknown") for r in rows}),
        "provider_row_counts": {
            name: sum(1 for r in rows if r.get("research_provider", "unknown") == name)
            for name in sorted({r.get("research_provider", "unknown") for r in rows})
        },
    }, indent=2))
    print(f"Saved {len(rows)}/{len(APPS)} rows. No paid fallback was used.")


if __name__ == "__main__":
    main()
