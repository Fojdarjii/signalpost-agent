#!/usr/bin/env python3
"""Evidence-first batch agent for Norwegian company numbers.

The Enhetsregisteret and roles endpoints are official public sources. Financial
records are attempted separately; a non-success response is recorded as
partial/missing and is never converted into a guessed value.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any

import httpx

BASE = "https://data.brreg.no"
ENHET = BASE + "/enhetsregisteret/api/enheter/{}"
ROLLER = BASE + "/enhetsregisteret/api/enheter/{}/roller"
REGNSKAP = BASE + "/regnskapsregisteret/regnskap/{}"
LIST = BASE + "/enhetsregisteret/api/enheter"
FINAL = {"ok", "not_found", "invalid_org_number", "identity_mismatch"}


class Budget:
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self._lock = asyncio.Lock()

    async def take(self) -> bool:
        async with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True


def valid_orgnr(value: str) -> bool:
    if not (value.isdigit() and len(value) == 9):
        return False
    weights = [3, 2, 7, 6, 5, 4, 3, 2]
    check = 11 - sum(int(digit) * weight for digit, weight in zip(value[:8], weights)) % 11
    check = 0 if check == 11 else check
    return check != 10 and check == int(value[8])


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fact(value: Any, url: str) -> dict[str, Any]:
    return {"value": value, "source_url": url, "retrieved_at": now()}


def missing(url: str, reason: str) -> dict[str, Any]:
    return {
        "value": None,
        "available": False,
        "reason": reason,
        "source_url": url,
        "retrieved_at": now(),
    }


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    semaphore: asyncio.Semaphore,
    budget: Budget,
    retries: int = 3,
) -> tuple[str, Any]:
    for attempt in range(retries):
        if not await budget.take():
            return "budget_exhausted", None
        try:
            async with semaphore:
                response = await client.get(url, headers={"Accept": "application/json"})
            if response.status_code == 200:
                return "ok", response.json()
            if response.status_code in (404, 410):
                return "not_found", None
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < retries:
                    await asyncio.sleep(2**attempt)
                    continue
            return "error", None
        except (httpx.HTTPError, ValueError):
            if attempt + 1 < retries:
                await asyncio.sleep(2**attempt)
    return "error", None


def person_name(person: dict[str, Any] | None) -> str | None:
    name = (person or {}).get("navn") or {}
    value = " ".join(
        part for part in (name.get("fornavn"), name.get("mellomnavn"), name.get("etternavn")) if part
    )
    return value or None


def parse_roles(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    roles = []
    for group in (data or {}).get("rollegrupper", []):
        for role in group.get("roller", []):
            if role.get("fratraadt") or role.get("avregistrert"):
                continue
            name = person_name(role.get("person"))
            if not name and role.get("enhet"):
                name = (role["enhet"].get("navn") or [None])[0]
            roles.append(
                {
                    "role": (role.get("type") or {}).get("beskrivelse"),
                    "group": (group.get("type") or {}).get("beskrivelse"),
                    "name": name,
                }
            )
    return roles


def parse_financials(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, list) or not data:
        return None
    accounts = sorted(
        data,
        key=lambda item: (item.get("regnskapsperiode") or {}).get("tilDato", ""),
        reverse=True,
    )
    account = accounts[0]
    result = account.get("resultatregnskapResultat") or {}
    operating = result.get("driftsresultat") or {}
    equity_debt = account.get("egenkapitalGjeld") or {}
    return {
        "period_from": (account.get("regnskapsperiode") or {}).get("fraDato"),
        "period_to": (account.get("regnskapsperiode") or {}).get("tilDato"),
        "currency": account.get("valuta"),
        "revenue": (operating.get("driftsinntekter") or {}).get("sumDriftsinntekter"),
        "operating_result": operating.get("driftsresultat"),
        "net_result": result.get("aarsresultat"),
        "equity": (equity_debt.get("egenkapital") or {}).get("sumEgenkapital"),
        "total_assets": (account.get("eiendeler") or {}).get("sumEiendeler"),
    }


def address(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    return ", ".join(
        part for part in [*(value.get("adresse") or []), value.get("postnummer"), value.get("poststed"), value.get("land")] if part
    )


async def build_profile(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    budget: Budget,
    orgnr: str,
) -> dict[str, Any] | None:
    if not valid_orgnr(orgnr):
        return {"orgnr": orgnr, "status": "invalid_org_number"}

    urls = {"company": ENHET.format(orgnr), "roles": ROLLER.format(orgnr), "financials": REGNSKAP.format(orgnr)}
    states = await asyncio.gather(*(get_json(client, url, semaphore, budget) for url in urls.values()))
    company_state, company = states[0]
    roles_state, roles = states[1]
    financial_state, financials = states[2]
    if company_state == "not_found":
        return {"orgnr": orgnr, "status": "not_found"}
    if company_state != "ok":
        return {"orgnr": orgnr, "status": "partial", "note": "main record unavailable"}
    if str(company.get("organisasjonsnummer")) != orgnr:
        return {"orgnr": orgnr, "status": "identity_mismatch"}

    profile: dict[str, Any] = {"orgnr": orgnr, "status": "ok", "facts": {}}
    facts = profile["facts"]
    facts["name"] = fact(company.get("navn"), urls["company"])
    facts["organisation_form"] = fact((company.get("organisasjonsform") or {}).get("beskrivelse"), urls["company"])
    facts["business_address"] = fact(address(company.get("forretningsadresse")), urls["company"])
    facts["postal_address"] = fact(address(company.get("postadresse")), urls["company"])
    facts["registration_date"] = fact(company.get("registreringsdatoEnhetsregisteret"), urls["company"])
    facts["founded_date"] = fact(company.get("stiftelsesdato"), urls["company"])
    facts["industry"] = fact(company.get("naeringskode1"), urls["company"])
    facts["employees"] = fact(company.get("antallAnsatte"), urls["company"])
    facts["website"] = fact(company.get("hjemmeside"), urls["company"])
    facts["status_flags"] = fact(
        {"bankrupt": company.get("konkurs"), "under_liquidation": company.get("underAvvikling"), "vat_registered": company.get("registrertIMvaregisteret")},
        urls["company"],
    )
    if roles_state == "ok" and parse_roles(roles):
        facts["roles"] = fact(parse_roles(roles), urls["roles"])
    elif roles_state == "not_found":
        facts["roles"] = missing(urls["roles"], "No roles data in official register")
    else:
        profile["status"] = "partial"
    parsed_financials = parse_financials(financials) if financial_state == "ok" else None
    if parsed_financials:
        facts["latest_financials"] = fact(parsed_financials, urls["financials"])
    elif financial_state == "not_found":
        facts["latest_financials"] = missing(urls["financials"], "No annual accounts available")
    else:
        profile["status"] = "partial"
    facts["shareholders"] = missing(urls["company"], "Not published in the open Brreg APIs")
    return profile


async def sample_orgnrs(
    client: httpx.AsyncClient, count: int, budget: Budget, cache_path: str
) -> list[str]:
    if os.path.exists(cache_path):
        cached = [line.strip() for line in open(cache_path, encoding="utf-8") if line.strip()]
        if len(cached) >= count:
            return cached[:count]
    numbers: list[str] = []
    for page in range(100):
        if len(numbers) >= count or not await budget.take():
            break
        response = await client.get(
            f"{LIST}?size=100&page={page}&konkurs=false&organisasjonsform=AS",
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            break
        items = (response.json().get("_embedded") or {}).get("enheter", [])
        if not items:
            break
        numbers.extend(str(item["organisasjonsnummer"]) for item in items if item.get("organisasjonsnummer"))
    numbers = numbers[:count]
    with open(cache_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(numbers))
    return numbers


async def main() -> None:
    parser = argparse.ArgumentParser(description="Collect evidence-backed Norwegian company profiles")
    parser.add_argument("--input", help="Text file with one organization number per line")
    parser.add_argument("--sample", type=int, help="Fetch this many AS companies from the official listing")
    parser.add_argument("--output", default="profiles.jsonl")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--budget", type=int, default=1900)
    args = parser.parse_args()
    if bool(args.input) == bool(args.sample):
        parser.error("provide exactly one of --input or --sample")
    if args.concurrency < 1 or args.budget < 1:
        parser.error("--concurrency and --budget must be positive")

    if args.sample is not None and args.sample < 1:
        parser.error("--sample must be positive")
    done = set()
    if os.path.exists(args.output):
        for line in open(args.output, encoding="utf-8"):
            try:
                row = json.loads(line)
                if row.get("status") in FINAL:
                    done.add(row["orgnr"])
            except (ValueError, KeyError):
                continue

    budget = Budget(args.budget)
    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=30) as client:
        if args.sample is not None:
            numbers = await sample_orgnrs(client, args.sample, budget, args.output + ".orgnrs.txt")
        else:
            numbers = [line.strip().replace(" ", "") for line in open(args.input, encoding="utf-8") if line.strip()]
        results = await asyncio.gather(
            *(build_profile(client, semaphore, budget, number) for number in numbers if number not in done)
        )
    results = [result for result in results if result is not None]
    with open(args.output, "a", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    finished = len(done) + sum(result["status"] in FINAL for result in results)
    print(f"Requests used: {budget.used}/{args.budget} | finished: {finished}/{len(numbers)}")


if __name__ == "__main__":
    asyncio.run(main())