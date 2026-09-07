from __future__ import annotations

import html as html_module
import json
import re
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from ..search_plan import SearchTask, build_search_url


@dataclass(frozen=True)
class ResultCard:
    source_job_id: str
    url: str
    posted_age_days: int | None


@dataclass(frozen=True)
class JobDetail:
    source_job_id: str
    title: str
    company: str
    description: str
    location: str
    employment_type: str
    posted_at: str
    apply_url: str


class _HTMLFacts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self.jsonld: list[str] = []
        self.text: list[str] = []
        self._script_jsonld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "a" and values.get("href"):
            self.links.append(values)
        if tag == "script" and "ld+json" in values.get("type", "").lower():
            self._script_jsonld = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._script_jsonld = False

    def handle_data(self, data: str) -> None:
        if self._script_jsonld:
            self.jsonld.append(data)
        self.text.append(data)


class PlatformAdapter(ABC):
    platform: str

    def build_search_url(self, task: SearchTask) -> str:
        return build_search_url(self.platform, task.query, task.age_days)

    def inspect_auth(self, html: str, url: str = "") -> str:
        if self.detect_challenge(html, url):
            return "challenged"
        lowered = (html + " " + url).casefold()
        return "auth_required" if any(token in lowered for token in self.login_tokens()) else "authenticated"

    def detect_challenge(self, html: str, url: str = "") -> bool:
        lowered = (html + " " + url).casefold()
        return any(token in lowered for token in ("verify you are human", "additional verification required", "captcha", "cf-chl", "security check"))

    def extract_result_cards(self, html: str, base_url: str) -> list[ResultCard]:
        facts = _HTMLFacts(); facts.feed(html)
        output: dict[str, ResultCard] = {}
        visible = " ".join(facts.text)
        age_match = re.search(r"(\d+)\s*(hour|day|week|month)s?\s+ago", visible, re.I)
        age = None
        if age_match:
            multiplier = {"hour": 0, "day": 1, "week": 7, "month": 30}[age_match.group(2).lower()]
            age = int(age_match.group(1)) * multiplier
        for attributes in facts.links:
            url = urllib.parse.urljoin(base_url, attributes["href"])
            source_id = self.extract_source_job_id(url, attributes)
            if source_id:
                output[source_id] = ResultCard(source_id, url, age)
        return list(output.values())

    def extract_job_detail(self, html: str, url: str) -> JobDetail:
        facts = _HTMLFacts(); facts.feed(html)
        posting: dict[str, Any] | None = None
        for raw in facts.jsonld:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            stack = value if isinstance(value, list) else [value]
            for item in stack:
                if isinstance(item, dict) and "JobPosting" in str(item.get("@type", "")):
                    posting = item; break
            if posting: break
        if not posting:
            raise ValueError("fixture has no JobPosting JSON-LD")
        organization = posting.get("hiringOrganization") or {}
        location = posting.get("jobLocation") or posting.get("jobLocationType") or ""
        if isinstance(location, list): location = location[0] if location else ""
        if isinstance(location, dict):
            address = location.get("address") or {}
            location = ", ".join(str(address.get(k, "")) for k in ("addressLocality", "addressRegion", "addressCountry") if address.get(k))
        return JobDetail(
            source_job_id=self.extract_source_job_id(url, {}), title=str(posting.get("title", "")).strip(),
            company=str(organization.get("name", "") if isinstance(organization, dict) else organization).strip(),
            description=html_module.unescape(re.sub(r"<[^>]+>", " ", str(posting.get("description", "")))).strip(),
            location=str(location).strip(), employment_type=str(posting.get("employmentType", "")).strip(),
            posted_at=str(posting.get("datePosted", "")).strip(), apply_url=str(posting.get("url", url)).strip(),
        )

    def detect_exhaustion(self, html: str) -> tuple[bool, str]:
        lowered = re.sub(r"\s+", " ", html).casefold()
        signal = next((x for x in ("no jobs found", "no results found", "no jobs match your search", "end of results", "you have viewed all jobs") if x in lowered), "")
        return bool(signal), signal

    @abstractmethod
    def extract_source_job_id(self, url: str, attrs: dict[str, str]) -> str: ...

    @abstractmethod
    def login_tokens(self) -> tuple[str, ...]: ...


class IndeedAdapter(PlatformAdapter):
    platform = "indeed"
    def extract_source_job_id(self, url: str, attrs: dict[str, str]) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        return str((query.get("jk") or query.get("vjk") or [attrs.get("data-jk", "")])[0])
    def login_tokens(self) -> tuple[str, ...]: return ("secure.indeed.com/auth", "/account/login", "sign in to indeed")


class LinkedInAdapter(PlatformAdapter):
    platform = "linkedin"
    def extract_source_job_id(self, url: str, attrs: dict[str, str]) -> str:
        match = re.search(r"/jobs/view/(\d+)", url)
        if match: return match.group(1)
        return str((urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("currentJobId") or [""])[0])
    def login_tokens(self) -> tuple[str, ...]: return ("/login", "/checkpoint", "/authwall")


class GlassdoorAdapter(PlatformAdapter):
    platform = "glassdoor"
    def extract_source_job_id(self, url: str, attrs: dict[str, str]) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if query.get("jl"): return str(query["jl"][0])
        match = re.search(r"/job-listing/(.+?)-JV_", url, re.I)
        return match.group(1) if match else ""
    def login_tokens(self) -> tuple[str, ...]: return ("/profile/login", "/member/login", "sign in to glassdoor")
