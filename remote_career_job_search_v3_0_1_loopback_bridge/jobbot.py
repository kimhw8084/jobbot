#!/usr/bin/env python3
"""Remote Career Job Search Automation v3.1.0 — Recall-First Enrichment + Verification + Exhaustive Ledger.

Design goals:
- retrieve broadly enough to sustain 10–20 strong applications/day and 500+ cumulative,
- keep every discovered job for dedupe/market intelligence,
- strictly separate role relevance, qualification fit, career value, and remote safety,
- never auto-submit applications or bypass website protections.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import functools
import concurrent.futures
import json
import re
import sqlite3
import sys
import tempfile
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import jobbot_core as c

VERSION = "3.1.0"
c.VERSION = VERSION

# Re-export core helpers used below.
clean_text=c.clean_text; strip_html=c.strip_html; norm=c.norm; parse_dt=c.parse_dt
posted_age_hours=c.posted_age_hours; annualized_salary=c.annualized_salary
extract_states=c.extract_states; is_restricted_url=c.is_restricted_url
canonical_url=c.canonical_url; validate_web_url=c.validate_web_url
csv_safe_cell=c.csv_safe_cell; now_iso=c.now_iso; utcnow=c.utcnow
abs_path=c.abs_path; load_toml=c.load_toml; host_of=c.host_of
Job=c.Job; HttpClient=c.HttpClient


@functools.lru_cache(maxsize=4096)
def _norm_cached_text(text:str)->str:
    # For semantic phrase matching, slash/hyphen-like punctuation is a separator, not part of a token.
    # Core URL/string normalization intentionally preserves '/', so normalize it here only.
    return re.sub(r"[/]+", " ", norm(text)).strip()

def phrase_present(phrase: str, text: str) -> bool:
    """Boundary-safe phrase matching; prevents SIS→analysis and Lean→clean.

    Normalized text is cached because a single job description is tested against dozens of phrases.
    """
    p=_norm_cached_text(str(phrase or "")); t=_norm_cached_text(str(text or ""))
    return bool(p and t and f" {p} " in f" {t} ")


def phrase_hits(phrases: Iterable[str], text: str) -> list[str]:
    return [p for p in phrases if phrase_present(p,text)]


def word_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#]+", norm(text))


def title_token_coverage(pattern: str, title: str) -> float:
    generic={"specialist","coordinator","associate","analyst","manager","senior","junior","lead","remote","the","and","of","for","to","in"}
    pt=[x for x in word_tokens(pattern) if x not in generic]
    tt=set(word_tokens(title))
    return (sum(1 for x in pt if x in tt)/len(pt)) if pt else 0.0


def extract_required_block(text: str, strategy: dict[str,Any]) -> str:
    low=text.lower(); cfg=strategy.get("strategy",{}).get("requirements",{})
    starts=[low.find(h.lower()) for h in cfg.get("required_section_headings",[]) if low.find(h.lower())>=0]
    if starts:
        start=min(starts); end=min(len(text),start+10000)
        for h in cfg.get("preferred_section_headings",[])+cfg.get("section_stop_headings",[]):
            i=low.find(h.lower(),start+5)
            if i>=0: end=min(end,i)
        return text[start:end]
    chunks=re.split(r"(?<=[.!?])\s+|\n+",text)
    markers=("required","must have","must possess","minimum","you have","you bring","what you need","what you'll need")
    return " ".join(x for x in chunks if any(m in x.lower() for m in markers))[:10000]


def extract_preferred_block(text: str, strategy: dict[str,Any]) -> str:
    low=text.lower(); cfg=strategy.get("strategy",{}).get("requirements",{})
    starts=[low.find(h.lower()) for h in cfg.get("preferred_section_headings",[]) if low.find(h.lower())>=0]
    if not starts: return ""
    start=min(starts); end=min(len(text),start+6000)
    for h in cfg.get("section_stop_headings",[]):
        i=low.find(h.lower(),start+5)
        if i>=0: end=min(end,i)
    return text[start:end]


def years_required(text: str) -> Optional[int]:
    low=text.lower(); vals=[]
    for pat in [
        r"(?:minimum(?: of)?\s*)?(\d{1,2})\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+)?experience",
        r"(?:minimum(?: of)?\s*)?(\d{1,2})\+?\s*(?:years?|yrs?)\s+(?:in|within|working)",
        r"experience\s*[:\-]?\s*(\d{1,2})\+?\s*(?:years?|yrs?)",
        # Common ATS phrasing: "8+ years of program management", "5 years of healthcare operations".
        r"(?:minimum(?: of)?\s*)?(\d{1,2})\+?\s*(?:years?|yrs?)\s+of\s+(?!age\b)(?:[a-z0-9&/+.-]+(?:\s+|$)){1,8}",
    ]:
        vals.extend(int(m.group(1)) for m in re.finditer(pat,low))
    return max(vals) if vals else None


_CRED_PATTERNS={
    "RN":[r"\bRN\b",r"registered nurse"], "LPN":[r"\bLPN\b",r"licensed practical nurse"],
    "LVN":[r"\bLVN\b",r"licensed vocational nurse"], "NP":[r"\bNP\b",r"nurse practitioner"],
    "MD":[r"\bMD\b",r"medical doctor",r"physician license"],
    # Deliberately do NOT match ordinary word "do".
    "DO":[r"\bD\.O\.\b",r"doctor of osteopathic",r"osteopathic physician"],
    "LCSW":[r"\bLCSW\b",r"licensed clinical social worker"],
    "LPC":[r"\bLPC\b",r"licensed professional counselor"],
    "LMFT":[r"\bLMFT\b",r"licensed marriage and family therapist"],
    "BCBA":[r"\bBCBA\b",r"board certified behavior analyst"],
    "CCS":[r"\bCCS\b"],"CPC":[r"\bCPC\b"],"CRC":[r"\bCRC\b"],"RHIT":[r"\bRHIT\b"],"RHIA":[r"\bRHIA\b"],
    "CNM":[r"\bCNM\b",r"certified nurse[- ]midwife",r"certified nurse midwife"],
    "IBCLC":[r"\bIBCLC\b",r"international board certified lactation consultant"],
    "CNA":[r"\bCNA\b",r"certified nursing assistant"],
    "CMA":[r"\bCMA\b",r"certified medical assistant"],
    "PT":[r"\bPT\b",r"physical therapist"], "OT":[r"\bOT\b",r"occupational therapist"],
    "SLP":[r"\bSLP\b",r"speech[- ]language pathologist"],
    "RDN":[r"\bRDN\b",r"registered dietitian nutritionist"], "RD":[r"\bRD\b",r"registered dietitian"],
    "PharmD":[r"\bPharmD\b",r"doctor of pharmacy"], "RPh":[r"\bRPh\b",r"registered pharmacist"],
}



def detect_required_credential(text: str, cred: str, strategy: dict[str,Any]) -> bool:
    req=extract_required_block(text,strategy) or text
    for pat in _CRED_PATTERNS.get(cred,[rf"\b{re.escape(cred)}\b"]):
        for m in re.finditer(pat,req,flags=re.I):
            win=req[max(0,m.start()-100):min(len(req),m.end()+100)].lower()
            if any(x in win for x in ("required","must","active","current","unrestricted","license","certification","credential")):
                return True
    return False


def source_confidence(job: Job, strategy: dict[str,Any]) -> float:
    cfg=strategy.get("strategy",{}).get("source_confidence",{})
    base=float(cfg.get(job.source_site,cfg.get("unknown",50)))
    vstat=clean_text(getattr(job,"source_verification",""))
    if vstat in {"verified_direct_ats","verified_canonical_ats","verified_jsonld"}: return max(base,96.0)
    if vstat=="direct_ats_link": return max(base,88.0)
    if vstat in {"identity_mismatch","unverified_discovery"}: return min(base,55.0)
    return base


def normalized_company_key(v:str)->str:
    t=norm(v)
    for x in (" inc "," llc "," ltd "," corporation "," corp "," company "," co "):
        t=(" "+t+" ").replace(x," ").strip()
    return t


def company_compatible(a:str,b:str)->bool:
    aa=normalized_company_key(a); bb=normalized_company_key(b)
    if not aa or not bb: return True
    if aa==bb or aa in bb or bb in aa: return True
    at=set(aa.split()); bt=set(bb.split())
    return bool(at and bt and len(at&bt)/max(1,min(len(at),len(bt)))>=0.7)


def employment_analysis(job:Job)->tuple[str,str]:
    """Classify employment arrangement conservatively; title/description override weak feed labels."""
    text=norm(" ".join([job.title,job.employment_type,job.description[:6000]]))
    title=norm(job.title); et=norm(job.employment_type)
    if any(phrase_present(x,text) for x in ("commission only","100% commission","commission-only")):
        return "commission_only","commission-only compensation"
    if any(phrase_present(x,text) for x in ("independent contractor","1099","freelance","contractor position","contractor role")) or "contractor" in title:
        return "independent_contractor","posting explicitly identifies contractor/1099/freelance arrangement"
    if any(phrase_present(x,text) for x in ("internship","intern position")) or re.search(r"\bintern\b",title,re.I):
        return "internship","internship"
    if any(phrase_present(x,text) for x in ("seasonal","temporary position","temporary role")):
        return "temporary","temporary/seasonal role"
    if any(phrase_present(x,text) for x in ("part-time","part time","variable-hour","variable hour")):
        return "part_time","part-time/variable-hour role"
    if re.search(r"\b(?:3|4|5|6|7|8|9|10|11|12|18|24)[- ]month contract\b|\bfixed[- ]term\b|\bcontract[- ]to[- ]hire\b|\bcontract (?:opportunity|role|position)\b",text,re.I):
        return "fixed_term_employee","fixed-term/contract arrangement stated"
    if "contract" in et and not any(x in et for x in ("full time permanent","permanent")):
        return "fixed_term_employee","source employment type is contract"
    if "full time" in et or "fulltime" in et or phrase_present("full-time",text):
        if "permanent" in et or phrase_present("permanent position",text): return "full_time_permanent","full-time permanent"
        return "full_time_employee","full-time role; no contractor marker detected"
    if "permanent" in et: return "full_time_permanent","permanent role"
    return "unknown","employment arrangement not explicit enough"


def source_verification_analysis(job:Job)->tuple[str,str,int]:
    """Separate discovery provenance from final employer/ATS verification."""
    url=job.apply_url or job.canonical_url; typ,token=ats_identity(url) if 'ats_identity' in globals() else ("","")
    raw=job.raw if isinstance(job.raw,dict) else {}
    if int(raw.get("_ledger_canonical_verified",0) or 0)==1:
        return clean_text(raw.get("_ledger_source_verification") or "verified_canonical_ats"), clean_text(raw.get("_ledger_source_verification_reason") or "preserved canonical verification from ledger"), 1
    discovery_company=clean_text(raw.get("_discovery_company") or raw.get("company") or raw.get("companyName") or "")
    enriched_company=clean_text(job.company)
    if discovery_company and enriched_company and not company_compatible(discovery_company,enriched_company):
        return "identity_mismatch",f"discovery company '{discovery_company}' does not match canonical company '{enriched_company}'",0
    if job.source_site in {"greenhouse","lever","ashby","smartrecruiters"}: return "verified_direct_ats",f"direct public {job.source_site} posting",1
    if typ and (raw.get("ats_enrichment") or raw.get("jsonld_enrichment")): return "verified_canonical_ats",f"discovery listing resolved to public {typ} posting",1
    if raw.get("jsonld_enrichment"): return "verified_jsonld","public employer page contained JobPosting structured data",1
    if typ: return "direct_ats_link",f"apply link points to public {typ} ATS but details were not fully enriched",0
    if job.source_site in {"linkedin","indeed","glassdoor","manual"}: return "assisted_board","user-assisted board capture; canonical employer verification still recommended",0
    return "unverified_discovery",f"{job.source_site or 'unknown'} is being used as discovery, not canonical proof",0


def recall_prefilter(job:Job,strategy:dict[str,Any])->tuple[bool,str]:
    """High-recall stage used ONLY to decide what deserves description/ATS enrichment."""
    cfg=strategy.get("strategy",{}).get("recall",{}); title=job.title
    if not title: return False,"missing title"
    exclusions=cfg.get("recall_exclusion_terms",[])
    if any(phrase_present(x,title) for x in exclusions): return False,"excluded occupation family"
    rules=strategy.get("strategy",{}).get("role_rules",{})
    if any(phrase_present(x,title) for x in rules.get("out_of_scope_title_terms",[])): return False,"explicit out-of-scope title"
    ht=phrase_hits(cfg.get("healthcare_title_markers",[]),title); rt=phrase_hits(cfg.get("role_markers",[]),title)
    if ht and rt: return True,f"healthcare/access title signals: {ht[0]} + {rt[0]}"
    # Some healthcare workflow nouns are sufficiently specific by themselves.
    for x in ("credentialing","enrollment","patient access","insurance verification","prior authorization","authorization and verification","medical records","health information","care coordinator","care partner","care navigator","care advocate","patient coordinator","member services","member support"):
        if phrase_present(x,title): return True,f"specific workflow title: {x}"
    for x in cfg.get("transferable_title_patterns",[]):
        if phrase_present(x,title): return True,f"transferable operations title: {x}"
    # Higher-ed and bilingual/content aliases.
    if re.search(r"\b(admissions|registrar|student records|student services|transcript|credential evaluator|application processor)\b",norm(title),re.I): return True,"higher-ed workflow title"
    if (phrase_present("Spanish",title) or phrase_present("bilingual",title)) and any(phrase_present(x,title) for x in ("support","reviewer","evaluator","specialist","coordinator")): return True,"bilingual transferable title"
    return False,"no recall-stage title archetype"


def source_remote_declared(job: Job) -> bool:
    return job.remote_status.lower()=="remote" or job.source_site in {"remotive","jobicy","remoteok","remotelanders"}


def remote_gate(job: Job, strategy: dict[str,Any], candidate: dict[str,Any]) -> tuple[str,str,float]:
    """Resolve the role's actual remote eligibility, giving role-specific text precedence over feed metadata.

    Conditional office rules for a different metro (e.g. "within 40 miles of Scottsdale") do not
    disqualify an Austin candidate when the same posting explicitly permits US-remote candidates.
    """
    rcfg=strategy.get("strategy",{}).get("remote",{})
    full=" ".join([job.location_raw,job.remote_status,job.description])
    sentences=[clean_text(x) for x in re.split(r"(?<=[.!?])\s+|\n+",full) if clean_text(x)]
    metros=[norm(x) for x in candidate.get("metro_terms",[]) if clean_text(x)]

    # Country/region eligibility. A remote-source label does not make a Philippines/EMEA-only role US-eligible.
    locblob=norm(" ".join([job.title,job.location_raw]))
    titleblob=norm(job.title)
    us_markers=("united states","usa","u s","us remote","remote us","texas","worldwide","anywhere","global")
    foreign_markers=("philippines","india","united kingdom","uk","europe","emea","latam","latin america","canada","mexico","australia","new zealand","apac","asia","singapore","romania","croatia","japan","taiwan")
    has_us=any(phrase_present(x,locblob) for x in us_markers)
    title_has_us=any(phrase_present(x,titleblob) for x in us_markers)
    foreign=[x for x in foreign_markers if phrase_present(x,locblob)]
    title_foreign=[x for x in foreign_markers if phrase_present(x,titleblob)]
    # Role-title geography is more specific than an aggregator's broad "USA" metadata.
    if title_foreign and not title_has_us:
        return "reject",f"job title explicitly targets a non-US region: {', '.join(title_foreign[:3])}",99.0
    if phrase_present("offshore",job.title) and not title_has_us:
        return "reject","job title explicitly identifies an offshore/non-US role",99.0
    if foreign and not has_us:
        return "reject",f"remote location is outside configured candidate country US: {', '.join(foreign[:3])}",99.0

    # Candidate-specific local/proximity requirements are authoritative blockers.
    for sent in sentences:
        low=norm(sent)
        if not any(x in low for x in ("office","onsite","on site","on-site","hybrid")): continue
        if any(mt in low for mt in metros) and re.search(r"(?:within|located|based|resid).{0,100}(?:office|onsite|on[- ]site|hybrid)|(?:office|onsite|on[- ]site|hybrid).{0,100}(?:within|located|based|resid)",low,re.I):
            return "reject","local proximity/office rule conflicts with remote-only requirement",98.0

    # Unconditional role-specific onsite/hybrid requirements. Ignore a geographically conditional
    # sentence for an obviously different metro if the posting also contains explicit remote language.
    explicit_remote=phrase_hits(rcfg.get("accepted_markers",[]),full)
    hard_patterns=[
        r"not (?:a )?fully remote(?: position| role)?",
        r"(?:onsite|on-site|in-person) (?:work )?(?:is )?required",
        r"hybrid (?:work )?(?:is )?required",
        r"\bhybrid(?:\s+work)?\s+(?:model|schedule)\b",
        r"must (?:report|commute|work).{0,100}(?:office|onsite|on-site)",
        r"expected to work (?:onsite|on-site|in[- ]office)\s+(?:three|four|five|[2-5])\s+days",
        r"(?:onsite|on-site|in[- ]office)\s+(?:three|four|five|[2-5])\s+days(?: per week| a week)?",
    ]
    for sent in sentences:
        low=norm(sent)
        if not any(re.search(p,sent,flags=re.I|re.S) for p in hard_patterns): continue
        conditional=bool(re.search(r"(?:if|when|for)\s+(?:you are |employees? |team members? )?(?:based|located|residing)|within\s+\d+\s+miles",sent,re.I))
        mentions_candidate=any(mt in low for mt in metros)
        if conditional and explicit_remote and not mentions_candidate:
            continue
        return "reject","posting text requires onsite/hybrid work for this candidate",100.0

    # Configured reject markers are still useful, but generic company-level 'hybrid work model'
    # should not beat explicit role-specific remote eligibility.
    for x in rcfg.get("reject_markers",[]):
        if not phrase_present(x,full):
            continue
        matching=[sent for sent in sentences if phrase_present(x,sent)]
        if explicit_remote and matching:
            applicable=[]
            for sent in matching:
                low=norm(sent)
                conditional=bool(re.search(r"(?:if|when|for)\s+(?:you are |employees? |team members? )?(?:based|located|residing)|within\s+\d+\s+miles",sent,re.I))
                if conditional and not any(mt in low for mt in metros):
                    continue
                applicable.append(sent)
            if not applicable:
                continue
        if x.lower()=="hybrid work model" and explicit_remote:
            continue
        return "reject",f"posting explicitly requires onsite/hybrid work: {x}",100.0

    # State-restricted remote eligibility. Explicit exclusions are checked before allow-lists.
    st=clean_text(candidate.get("state") or "TX").upper()
    excluded_patterns=re.findall(r"(?:not (?:considering|hiring) candidates?(?: residing| located)? in|cannot hire in|unable to hire in|not available in|remote except(?: in)?|excluding)\s+([^.;]{2,220})",full,flags=re.I)
    excluded_states=set()
    for p in excluded_patterns: excluded_states |= extract_states(p)
    if st in excluded_states: return "reject",f"posting explicitly excludes candidate state {st}",98.0
    state_patterns=re.findall(r"(?:must (?:live|reside|be located)|residents? of|remote (?:in|from)|eligible states?|based in|open to candidates in)\s+([^.;]{2,220})",full,flags=re.I)
    if state_patterns:
        states=set()
        for p in state_patterns: states |= extract_states(p)
        if states and st not in states: return "reject",f"remote restricted to states excluding {st}: {', '.join(sorted(states))}",95.0

    if explicit_remote: return "pass",f"explicit remote language: {explicit_remote[0]}",98.0
    if source_remote_declared(job): return "pass","source/structured metadata declares remote; no posting contradiction found",78.0
    return "review","remote status not explicit enough",40.0

def parse_deadline(text: str) -> tuple[str,str]:
    for pat in [
        r"application deadline\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
        r"posting close date\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
        r"applications? (?:will be )?accepted (?:at least )?until\s+([A-Za-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
        r"accepted through\s+([A-Za-z]+\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})",
    ]:
        m=re.search(pat,text,flags=re.I)
        if m:
            dt=parse_dt(m.group(1))
            if dt: return dt.date().isoformat(),("closed" if dt.date()<utcnow().date() else "active")
    return "","unknown"


def canonical_job_id(company: str, title: str, location: str, source_id: str="") -> str:
    loc=norm(location)
    remote_bucket="remote-us" if (not loc or any(phrase_present(x,loc) for x in ("remote","usa","united states","anywhere"))) else loc
    core="|".join([norm(company),norm(title),remote_bucket])
    # A source requisition/id prevents two same-title openings at one employer from collapsing.
    # Cross-source mirrors still merge through job-specific URLs or strong content fingerprints.
    if clean_text(source_id): core+="|sid:"+norm(source_id)
    elif not norm(company) or not norm(title): core+="|anonymous"
    return "J"+hashlib.sha256(core.encode("utf-8",errors="ignore")).hexdigest()[:14].upper()

# Make the core Job.job_id property use canonical ledger identity logic.
c.canonical_job_id=canonical_job_id


def capabilities(candidate: dict[str,Any]) -> list[dict[str,Any]]:
    v=candidate.get("capabilities",[])
    return v if isinstance(v,list) else []


def capability_for(term: str, candidate: dict[str,Any]) -> tuple[str,str]:
    rank={"not_evidenced":0,"training":1,"strong_transfer":2,"proven":3}; best=("not_evidenced","")
    for cap in capabilities(candidate):
        level=clean_text(cap.get("level") or "not_evidenced")
        aliases=[clean_text(cap.get("name"))]+[clean_text(x) for x in cap.get("aliases",[])]
        if any(a and (phrase_present(a,term) or phrase_present(term,a)) for a in aliases):
            if rank.get(level,0)>rank.get(best[0],0): best=(level,clean_text(cap.get("name")))
    return best


def classify_role(job: Job, strategy: dict[str,Any], mode: str) -> tuple[Optional[dict[str,Any]],list[str],float,str,float]:
    """Recall-first occupational classification.

    v2.1 deliberately separates *role recall* from final qualification. A broad but plausible
    coordinator/specialist title can enter a profile with moderate relevance so its ATS description
    can be evaluated; it cannot reach Apply Now without qualification, source and employment gates.
    """
    scfg=strategy.get("strategy",{}); rules=scfg.get("role_rules",{}); title=clean_text(job.title); desc=strip_html(job.description or "")
    tnorm=norm(title)
    # Fundamental occupations are never rescued by generic words such as quality/operations/support.
    if any(phrase_present(x,title) for x in rules.get("out_of_scope_title_terms",[])): return None,[],0.0,"",0.0
    if any(re.search(rf"\b{re.escape(x)}\b", title, flags=re.I) for x in rules.get("engineering_terms",[])): return None,[],0.0,"",0.0
    if re.search(r"\b(?:director|vice president|vp|chief|cto|cfo|coo|cmo|cio)\b",title,flags=re.I): return None,[],0.0,"",0.0
    # Clinical credentials in a title are occupational identity, not generic keyword evidence.
    # Keep short abbreviations case-sensitive so ordinary words (notably "do") never become credentials.
    if re.search(r"\b(?:RN|LPN|LVN|NP|MD|DO|CNM|IBCLC|CNA|CMA|LCSW|LPC|LMFT|BCBA|PT|OT|SLP|RDN|RD|RPh)\b|\bPharmD\b",title): return None,[],0.0,"",0.0
    if re.search(r"\b(?:nurse|registered nurse|licensed practical nurse|licensed vocational nurse|nurse practitioner|nurse midwife|certified nurse|physician|physician assistant|psychiatrist|neurologist|surgeon|therapist|clinician|pharmacist|physical therapist|occupational therapist|speech[- ]language pathologist|dietitian)\b",title,flags=re.I): return None,[],0.0,"",0.0
    # The near-term strategy is deliberately non-sales/non-business-development, even inside healthcare.
    if re.search(r"\b(?:sales|sales executive|sales representative|sales consultant|account executive|territory manager|business development|sales manager)\b",title,flags=re.I): return None,[],0.0,"",0.0
    if re.search(r"clinical operations (?:lead|manager)|clinical trial manager", title, flags=re.I) and re.search(r"\b(?:CRA|clinical trial|investigator site|GCP|protocol deviation)\b", desc, flags=re.I): return None,[],0.0,"",0.0
    recall_cfg=scfg.get("recall",{})
    foreign_langs=("mandarin","chinese","french","german","dutch","finnish","danish","swedish","croatian","czech","romanian","greek","thai","vietnamese","ukrainian","turkish","slovak","slovenian","latvian","lithuanian","hebrew","hungarian","estonian","bulgarian","portuguese","italian")
    if any(phrase_present(x,title) for x in foreign_langs) and not phrase_present("Spanish",title) and not phrase_present("English",title):
        return None,[],0.0,"",0.0
    health_in_title=bool(phrase_hits(recall_cfg.get("healthcare_title_markers",[]),title))
    if not health_in_title and any(phrase_present(x,title) for x in recall_cfg.get("recall_exclusion_terms",[])):
        return None,[],0.0,"",0.0

    profiles=[p for p in strategy.get("searches",[]) if p.get("enabled",True)]
    allowed=set(scfg.get("run_modes",{}).get(mode,{}).get("profiles",[]))
    if allowed: profiles=[p for p in profiles if p.get("name") in allowed]
    byname={clean_text(p.get("name")):p for p in profiles}
    best=None; best_hits=[]; best_rel=0.0; best_family=""; best_domain=0.0

    # Stage A: configured exact/near-exact titles and generic title + responsibility patterns.
    for p in profiles:
        dm=p.get("domain_markers",[])
        dtitle=phrase_hits(dm,title); dfront=phrase_hits(dm,desc[:3000]); dany=phrase_hits(dm,desc)
        dscore=100.0 if dtitle else (88.0 if dfront else (58.0 if dany else 0.0))
        if p.get("domain") in {"transferable","content"}: dscore=max(dscore,75.0)
        direct=[]
        for kw in p.get("keywords",[]):
            if phrase_present(kw,title): direct.append((kw,100.0))
            else:
                cov=title_token_coverage(kw,title)
                if cov>=.80: direct.append((kw,92.0))
                elif cov>=.67 and len([z for z in word_tokens(kw) if len(z)>3])>=2: direct.append((kw,84.0))
        generic=[g for g in p.get("generic_title_patterns",[]) if phrase_present(g,title)]
        resp=phrase_hits(p.get("responsibility_markers",[]),desc[:7000])
        rel=0.0; family=""; hits=[]
        if direct:
            rel=max(v for _,v in direct); family=sorted(direct,key=lambda z:-z[1])[0][0]; hits=[k for k,_ in sorted(direct,key=lambda z:-z[1])[:6]]
            inherent=any(phrase_present(x,title) for x in ("patient","clinical","medical","healthcare","health information","provider","payer","credentialing"))
            # Enrollment/intake/onboarding are intentionally allowed through title-only recall; domain is verified later.
            title_workflow=any(phrase_present(x,title) for x in ("enrollment","intake","onboarding","credentialing","patient access","insurance verification","prior authorization","care coordinator","care partner","care navigator","care advocate","member services"))
            if p.get("domain")=="healthcare" and dscore<50 and not inherent and not title_workflow: rel=0.0
        elif generic:
            inherent=bool(phrase_hits(dm,title)) or any(phrase_present(x,title) for x in ("patient","member","provider","medical","healthcare","credentialing","enrollment"))
            if p.get("domain")!="healthcare":
                rel=74.0+(min(8,len(resp)*2) if resp else 0); family=generic[0]; hits=[generic[0]]
            elif (dscore>=50 or inherent) and (resp or inherent):
                rel=78.0+min(8,len(resp)*2); family=generic[0]; hits=[generic[0]]
        if rel:
            rel=min(100.0,rel+max(0,3-int(p.get("priority",3)))*1.5)
        if rel>best_rel: best,best_hits,best_rel,best_family,best_domain=p,hits,rel,family,dscore

    # Stage B: explicit title archetypes recovered even when a broad source provided no description.
    def choose(profile_name:str,rel:float,family:str,domain:float=75.0):
        nonlocal best,best_hits,best_rel,best_family,best_domain
        p=byname.get(profile_name)
        if p and rel>best_rel:
            best=p; best_hits=[family]; best_rel=rel; best_family=family; best_domain=domain

    health_specific=[
        "credentialing associate","credentialing specialist","credentialing coordinator","credentialing and enrollment coordinator",
        "patient access","patient enrollment","healthcare enrollment","provider enrollment","payer enrollment","member enrollment",
        "insurance verification","prior authorization","authorization and verification","referral coordinator","medical records","health information",
        "care coordinator","care partner","care navigator","care advocate","patient coordinator","patient support","patient services",
        "member support","member services","provider services","provider relations","healthcare coordinator","patient registration",
    ]
    for x in health_specific:
        if phrase_present(x,title):
            target="P0-health-information-quality-access" if any(k in x for k in ("credential","verification","authorization","records","health information","referral")) else "P0-healthcare-operations-access"
            choose(target,90.0,x,92.0); break
    if re.search(r"\b(enrollment specialist|enrollment coordinator|intake specialist|intake coordinator|scheduling coordinator|registration specialist)\b",tnorm,re.I):
        choose("P0-healthcare-operations-access",80.0,"enrollment/intake/registration workflow",55.0)
    if re.search(r"\b(care coordinator|care navigator|care advocate|member services representative|member support representative)\b",tnorm,re.I):
        choose("P0-volume-healthcare-support",88.0,"patient/member support workflow",90.0)
    if re.search(r"\b(data operations|data quality|data integrity|data validation)\b",tnorm,re.I):
        choose("P1-transferable-data-ops",90.0,"data quality/operations",80.0)
    # Generic operations bridge excludes occupationally specialized subfields.
    recall_cfg=scfg.get("recall",{})
    specialized=any(phrase_present(x,title) for x in recall_cfg.get("recall_exclusion_terms",[]))
    if not specialized and any(phrase_present(x,title) for x in recall_cfg.get("transferable_title_patterns",[])):
        patt=next((x for x in recall_cfg.get("transferable_title_patterns",[]) if phrase_present(x,title)),"transferable operations")
        if any(phrase_present(x,title) for x in ("implementation specialist","implementation coordinator","onboarding specialist","project coordinator","program coordinator")):
            health_domain=any(phrase_present(x,title+" "+desc[:3000]) for x in ("healthcare","patient","clinical","medical","provider","health plan","telehealth","EHR","EMR"))
            choose("P1-healthcare-implementation-project" if health_domain else "P1-transferable-operations",84.0 if health_domain else 78.0,patt,88.0 if health_domain else 75.0)
        else:
            choose("P1-transferable-operations",78.0,patt,75.0)
    # Higher-ed workflow titles.
    if re.search(r"\b(admissions|registrar|student records|student services|transcript|credential evaluator|application processor)\b",tnorm,re.I):
        choose("P2-higher-ed-edtech",88.0,"higher-ed records/enrollment workflow",90.0)
    # Content lane: English/Spanish/generic only. Do not promote clearly foreign-language-only roles.
    foreign_only=any(phrase_present(x,title) for x in foreign_langs) and not phrase_present("Spanish",title) and not phrase_present("English",title)
    if not foreign_only and any(phrase_present(x,title) for x in ("content reviewer","content evaluator","AI evaluator","Spanish content","bilingual content","language reviewer")):
        choose("P3-content-ai-quality",82.0,"content/language quality",80.0)

    return best,best_hits,best_rel,best_family,best_domain


def pick_profile(job: Job, strategy: dict[str,Any], mode: str):
    p,h,r,_,_=classify_role(job,strategy,mode); return p,h,r/100.0


def requirement_analysis(job: Job, profile: dict[str,Any], strategy: dict[str,Any], candidate: dict[str,Any]) -> dict[str,Any]:
    req=extract_required_block(job.description,strategy); pref=extract_preferred_block(job.description,strategy)
    rcfg=strategy.get("strategy",{}).get("requirements",{})
    req_skills=[x for x in rcfg.get("critical_skill_catalog",[]) if phrase_present(x,req)]
    pref_skills=[x for x in rcfg.get("critical_skill_catalog",[]) if phrase_present(x,pref)]
    matches=[]; learnable=[]; critical=[]
    for sk in req_skills:
        level,_=capability_for(sk,candidate)
        if level=="proven": matches.append(f"{sk}: proven")
        elif level=="strong_transfer": matches.append(f"{sk}: strong transfer")
        elif level=="training": learnable.append(f"{sk}: training only")
        else: learnable.append(f"{sk}: not evidenced")
    for label,skills in [
        ("healthcare interoperability",["HL7","FHIR","CCDA","ADT"]),
        ("software engineering",["Java","JavaScript","TypeScript","React","AWS","Azure","GCP","Apex"]),
        ("healthcare quality measurement",["HEDIS","Stars","NCQA"]),
    ]:
        miss=[s for s in skills if s in req_skills and capability_for(s,candidate)[0] in {"training","not_evidenced"}]
        if len(miss)>=2: critical.append(f"core {label} requirements not evidenced: {', '.join(miss)}")
    domain_checks=[
        ("finance/accounting",["accounting","financial analysis","FP&A","revenue recognition","ASC 606","CPA"]),
        ("software engineering",["software development","software engineering","Java","Apex","React","Kubernetes"]),
        ("cybersecurity",["cybersecurity","vulnerability management","penetration testing","OWASP","Burp Suite"]),
        ("sales",["sales quota","closing business","full sales cycle","account executive"]),
        ("clinical trials",["clinical trial management","clinical research","clinical trial manager"]),
        ("recruiting",["recruiting operations","talent acquisition","sourcing"]),
    ]
    for label,terms in domain_checks:
        if any(phrase_present(t,req) for t in terms) and re.search(r"\b(?:\d+\+?\s+years?|experience|required|must)\b",req,flags=re.I):
            # Candidate config explicitly encodes unsupported fundamental fields.
            aliases={"finance/accounting":"finance accounting","software engineering":"software engineering","cybersecurity":"cybersecurity"}
            level,_=capability_for(aliases.get(label,label),candidate)
            if level=="not_evidenced": critical.append(f"required {label} experience not evidenced")
    if re.search(r"bachelor'?s degree|baccalaureate|bs degree|ba degree",req,flags=re.I):
        specific=[x for x in ("accounting","finance","computer science","engineering","nursing","statistics") if phrase_present(x,req)]
        ed=" ".join(candidate.get("education",[]))
        if specific and not any(phrase_present(x,ed) for x in specific): learnable.append(f"specific degree field requested: {', '.join(specific)}")
    mgmt_text=(req+" "+job.description[:9000])
    title_management=bool(re.search(r"\b(?:supervisor|people lead|team lead|people manager)\b",job.title,flags=re.I))
    mgmt=title_management or any(phrase_present(x,mgmt_text) for x in rcfg.get("management_phrases",[])) or bool(re.search(r"\b(?:manage|lead|supervise|supervising|coach|coaching)\s+(?:a\s+)?(?:team|staff|employees?|specialists?|associates?|coordinators?|case managers?)\b|\bdirect reports?\b|\bhiring.{0,80}coaching\b",mgmt_text,flags=re.I|re.S))
    if mgmt and float(candidate.get("people_management_years",0) or 0)<=0: critical.append("people-management responsibility without documented people-management experience")
    yrs=years_required(req or job.description); pname=profile.get("name","")
    avail=float(candidate.get("relevant_operations_years",0) or 0)
    if "quality-data" in pname: avail=max(float(candidate.get("professional_data_analytics_years",0) or 0),1.0)
    elif "implementation" in pname: avail=max(float(candidate.get("professional_implementation_years",0) or 0),1.5)
    elif "higher-ed" in pname: avail=float(candidate.get("higher_ed_years",0) or 0)
    elif "healthcare" in pname: avail=max(float(candidate.get("direct_healthcare_years",0) or 0),float(candidate.get("relevant_operations_years",0) or 0))
    if yrs is not None:
        if yrs>avail+3: critical.append(f"requires {yrs}+ years; directly relevant evidence is materially lower")
        elif yrs>avail+1: learnable.append(f"requires {yrs}+ years; experience is somewhat below requirement")
        else: matches.append(f"years requirement ({yrs}+) is within/near evidenced range")
    return {"required_text":req,"preferred_text":pref,"required_skills":req_skills,"preferred_skills":pref_skills,"matches":sorted(set(matches)),"learnable_gaps":sorted(set(learnable)),"critical_gaps":sorted(set(critical)),"management_required":mgmt,"years_required":yrs}


def qualification_score(job: Job, profile: dict[str,Any], a: dict[str,Any], candidate: dict[str,Any]) -> float:
    base={
        "P0-healthcare-operations-access":90,"P0-health-information-quality-access":82,"P0-volume-healthcare-support":88,
        "P1-healthcare-quality-data":64,"P1-healthcare-implementation-project":62,"P1-transferable-data-ops":72,
        "P1-transferable-operations":78,"P2-higher-ed-edtech":88,"P3-content-ai-quality":68,
    }.get(profile.get("name",""),60)
    q=float(base)
    if a.get("years_required"):
        if any("materially lower" in x for x in a["critical_gaps"]): q-=25
        elif any("somewhat below" in x for x in a["learnable_gaps"]): q-=10
        else: q+=3
    if a.get("management_required") and float(candidate.get("people_management_years",0) or 0)<=0: q-=28
    for sk in a.get("required_skills",[]):
        q += {"proven":2,"strong_transfer":0,"training":-5,"not_evidenced":-10}.get(capability_for(sk,candidate)[0],-10)
    for g in a.get("critical_gaps",[]):
        if "people-management" not in g and "requires " not in g: q-=16
    if phrase_present("senior manager",job.title): q-=16
    elif re.search(r"\bmanager\b",job.title,flags=re.I): q-=10
    elif re.search(r"\bsenior\b",job.title,flags=re.I): q-=5
    return max(0,min(100,q))


def extraction_confidence(job: Job) -> float:
    s=25+(15 if job.title else 0)+(10 if job.company else 0)+(30 if len(job.description or "")>800 else 15 if len(job.description or "")>250 else 0)+(10 if job.apply_url else 0)+(5 if job.posted_at else 0)+(5 if job.location_raw else 0)
    return min(100,float(s))


def career_score(job: Job, profile: dict[str,Any], strategy: dict[str,Any]) -> float:
    lane=profile.get("career_lane",""); text=job.title+" "+job.description
    v={"healthcare_access":72,"healthcare_growth_bridge":86,"transferable_data_ops":72,"transferable_operations":64,"higher_ed_edtech_hedge":66,"content_ai_opportunistic":55}.get(lane,55)
    acc=phrase_hits(strategy.get("strategy",{}).get("signals",{}).get("career_accelerators",[]),text); v+=min(12,len(acc)*2)
    if phrase_present("quality",text) or phrase_present("data quality",text): v+=4
    if phrase_present("implementation",text) or phrase_present("process improvement",text): v+=4
    if phrase_present("SQL",text) or phrase_present("Power BI",text): v+=2
    ann=annualized_salary(job.salary_min,job.salary_max,job.salary_period)
    if ann is not None: v += 5 if ann>=80000 else 3 if ann>=65000 else -8 if ann<45000 else 0
    if lane=="healthcare_access" and not any(phrase_present(x,text) for x in ("quality","data","process improvement","implementation","reporting","audit")): v-=5
    return max(0,min(100,float(v)))



def extract_travel_percent(text:str)->Optional[float]:
    vals=[]
    for pat in [r"travel(?:ing)?(?: requirements?)?[^.%]{0,80}?(\d{1,3})\s*%",r"up to\s+(\d{1,3})\s*%\s+travel",r"travel\s+(\d{1,3})\s*%"]:
        vals += [float(m.group(1)) for m in re.finditer(pat,text,flags=re.I)]
    return max(vals) if vals else None


def extract_timezone_requirement(text:str)->str:
    pats=[r"(?:work|working|operate|available|hours)[^.;]{0,100}\b(Pacific|Eastern|Central|Mountain)\s+(?:Time|business hours)",r"\b(PST|PDT|EST|EDT|CST|CDT|MST|MDT)\b[^.;]{0,60}(?:hours|schedule|required)"]
    for pat in pats:
        m=re.search(pat,text,flags=re.I)
        if m: return clean_text(m.group(0))[:220]
    return ""


def work_auth_analysis(text:str,candidate:dict[str,Any])->tuple[str,str]:
    low=text.lower(); req=""
    markers=["without sponsorship","unable to sponsor","no sponsorship","will not sponsor","cannot sponsor","authorized to work for any employer","must be authorized to work","visa sponsorship is not available"]
    for m in markers:
        i=low.find(m)
        if i>=0: req=clean_text(text[max(0,i-100):i+220]); break
    if not req: return "unknown",""
    status=clean_text(candidate.get("work_authorization") or "unknown").lower()
    if status in {"authorized","authorized_no_sponsorship","us_citizen","permanent_resident","no_sponsorship_needed"}: return "pass",req
    if status in {"needs_sponsorship","requires_sponsorship"}: return "reject",req
    return "review",req


def application_friction_score(job:Job)->float:
    text=(job.description or "").lower(); v=100.0
    if not job.apply_url: v-=35
    elif is_restricted_url(job.apply_url): v-=15
    for phrase,pen in [("cover letter",8),("professional references",8),("three professional references",12),("work sample",12),("writing sample",10),("take-home",12),("assessment",6),("voice recording",18),("video recording",18),("portfolio required",10)]:
        if phrase in text: v-=pen
    return max(20.0,min(100.0,v))


def urgency_score(job:Job)->float:
    age=posted_age_hours(job.posted_at); v=40.0 if age is None else 100.0 if age<24 else 88.0 if age<=72 else 70.0 if age<=168 else 48.0 if age<=336 else 25.0 if age<=720 else 8.0
    dl=clean_text(getattr(job,"application_deadline","")); dt=parse_dt(dl) if dl else None
    if dt:
        days=(dt.date()-utcnow().date()).days
        if 0<=days<=2: v=max(v,100)
        elif days<=7: v=max(v,90)
        elif days<=14: v=max(v,75)
    return max(0,min(100,v))


def score_job(job: Job, strategy: dict[str,Any], candidate: dict[str,Any]) -> Job:
    # Every downstream parser works on normalized visible text, never raw ATS HTML.
    job.description=strip_html(job.description or "")
    mode=getattr(job,"_mode","fast"); profile,kws,rel,family,dscore=classify_role(job,strategy,mode)
    job.relevance_score=round(rel,1); job.normalized_title_family=family or ""; job.domain_score=round(dscore,1)
    job.application_deadline,job.posting_status=parse_deadline(job.description)
    job.remote_gate,job.remote_gate_reason,job.remote_confidence=remote_gate(job,strategy,candidate)
    job.employment_class,job.employment_reason=employment_analysis(job)
    job.source_verification,job.source_verification_reason,job.canonical_verified=source_verification_analysis(job)
    job.source_confidence=round(source_confidence(job,strategy),1); job.extraction_confidence=round(extraction_confidence(job),1)
    job.travel_percent=extract_travel_percent(job.description); job.timezone_requirement=extract_timezone_requirement(job.description)
    job.work_auth_gate,job.work_authorization_requirement=work_auth_analysis(job.description,candidate)
    job.application_friction_score=round(application_friction_score(job),1); job.urgency_score=round(urgency_score(job),1)
    amin=annualized_salary(job.salary_min,job.salary_max,job.salary_period)
    if job.salary_min is not None and job.salary_max is not None:
        mn=annualized_salary(job.salary_min,job.salary_min,job.salary_period); mx=annualized_salary(job.salary_max,job.salary_max,job.salary_period); job.salary_annual_mid=((mn+mx)/2 if mn is not None and mx is not None else amin)
    else: job.salary_annual_mid=amin
    job.application_priority_score=0.0; job.eligibility_confidence=0.0
    _,job.recall_reason=recall_prefilter(job,strategy)
    if not profile:
        job.search_profile=""; job.career_lane=""; job.resume_variant=""; job.matched_keywords=[]; job.qualification_score=0.0
        job.requirement_matches=[]; job.requirement_gaps=[]; job.required_skills=[]; job.management_required=0
        job.landing_score=job.career_score=job.door_score=0.0; job.recommendation="OUT_OF_SCOPE"; job.hard_reject_reasons=[]; job.score_reasons=["role-family relevance below threshold / out of scope"]
        return job
    job.search_profile=clean_text(profile.get("name")); job.career_lane=clean_text(profile.get("career_lane")); job.resume_variant=clean_text(profile.get("resume_variant")); job.matched_keywords=kws
    a=requirement_analysis(job,profile,strategy,candidate); job.requirement_matches=a["matches"]; job.requirement_gaps=a["critical_gaps"]+a["learnable_gaps"]; job.required_skills=a["required_skills"]; job.management_required=1 if a["management_required"] else 0; job.years_required=a["years_required"]
    q=qualification_score(job,profile,a,candidate); job.qualification_score=round(q,1)
    reasons=[]
    if job.remote_gate=="reject": reasons.append(job.remote_gate_reason)
    if job.posting_status=="closed": reasons.append(f"application deadline passed: {job.application_deadline}")
    if job.work_auth_gate=="reject": reasons.append("work-authorization requirement conflicts with configured candidate status")
    if job.source_verification=="identity_mismatch": reasons.append(job.source_verification_reason)
    if job.employment_class in {"commission_only","internship"}: reasons.append(job.employment_reason)
    max_travel=float(candidate.get("max_travel_percent",-1) or -1)
    if max_travel>=0 and job.travel_percent is not None and job.travel_percent>max_travel: reasons.append(f"travel requirement {job.travel_percent:.0f}% exceeds configured maximum {max_travel:.0f}%")
    floor=float(candidate.get("minimum_salary_annual",0) or 0)
    if floor>0 and job.salary_annual_mid is not None and job.salary_annual_mid<floor: reasons.append(f"annualized compensation below configured floor ${floor:,.0f}")
    rules=strategy.get("strategy",{}).get("role_rules",{})
    for term in rules.get("hard_seniority_terms",[]):
        if phrase_present(term,job.title): reasons.append(f"seniority beyond near-term strategy: {term}")
    for cred in strategy.get("strategy",{}).get("filters",{}).get("hard_reject_required_credentials",[]):
        if detect_required_credential(job.description,cred,strategy): reasons.append(f"required credential not documented in resume: {cred}")
    for x in strategy.get("strategy",{}).get("filters",{}).get("hard_reject_phrases",[]):
        if phrase_present(x,job.title+" "+job.description): reasons.append(f"hard-exclusion phrase: {x}")
    job.hard_reject_reasons=sorted(set(reasons))
    fresh=job.urgency_score; friction=job.application_friction_score
    soft_hits=phrase_hits(strategy.get("strategy",{}).get("filters",{}).get("soft_penalty_phrases",[]),job.title+" "+job.description+" "+job.employment_type)
    if job.travel_percent is not None and job.travel_percent>=25: soft_hits=sorted(set(soft_hits+[f"travel {job.travel_percent:.0f}%"]))
    landing=.70*q+.12*fresh+.05*friction+.05*job.source_confidence+.05*job.remote_confidence+.03*50-min(14,len(soft_hits)*3.5)
    if job.work_auth_gate=="review": landing-=4
    job.landing_score=round(max(0,min(100,landing)),1); job.career_score=round(max(0,career_score(job,profile,strategy)-min(10,len(soft_hits)*2)),1); job.door_score=round(.70*job.landing_score+.30*job.career_score,1)
    job.application_priority_score=round(.72*job.door_score+.18*job.urgency_score+.10*job.application_friction_score,1)
    job.eligibility_confidence=round((job.relevance_score+job.qualification_score+job.remote_confidence+job.extraction_confidence)/4,1)
    sc=strategy.get("strategy",{}).get("scoring",{}); critical=a["critical_gaps"]
    stable_employment=job.employment_class in {"full_time_employee","full_time_permanent"}
    verified=bool(job.canonical_verified)
    if job.hard_reject_reasons: job.recommendation="SKIP_HARD_GATE"
    elif job.source_verification in {"identity_mismatch"}: job.recommendation="SKIP_SOURCE"
    elif job.employment_class in {"independent_contractor","freelance","temporary","seasonal","fixed_term_employee"}: job.recommendation="CONTRACT_REVIEW"
    elif job.employment_class=="part_time": job.recommendation="PART_TIME_REVIEW"
    elif job.employment_class=="unknown": job.recommendation="REVIEW_EMPLOYMENT"
    elif not verified: job.recommendation="VERIFY_SOURCE"
    elif job.remote_gate=="review": job.recommendation="REVIEW_REMOTE"
    elif job.work_auth_gate=="review": job.recommendation="REVIEW"
    elif rel>=float(sc.get("minimum_relevance_for_apply",80)) and q>=float(sc.get("minimum_qualification_for_apply",72)) and job.landing_score>=float(sc.get("minimum_landing_for_apply",74)) and job.career_score>=float(sc.get("minimum_career_for_apply",55)) and not critical and not soft_hits and stable_employment: job.recommendation="APPLY_NOW"
    elif rel>=72 and q>=float(sc.get("minimum_landing_for_volume_apply",68)) and job.landing_score>=float(sc.get("minimum_landing_for_volume_apply",68)) and job.career_score>=float(sc.get("minimum_career_for_volume_apply",50)) and not critical and stable_employment: job.recommendation="APPLY_VOLUME"
    elif rel>=76 and job.career_score>=float(sc.get("high_value_stretch_min_career",78)) and job.landing_score>=float(sc.get("high_value_stretch_min_landing",52)) and not critical and stable_employment: job.recommendation="HIGH_VALUE_STRETCH"
    elif job.door_score>=float(sc.get("review_final_score",62)): job.recommendation="REVIEW"
    else: job.recommendation="LOW_PRIORITY"
    sig=strategy.get("strategy",{}).get("signals",{}); text=job.title+" "+job.description
    job.matched_positive=phrase_hits(sig.get("strong_positive",[]),text); job.matched_accelerators=phrase_hits(sig.get("career_accelerators",[]),text); job.matched_bilingual=phrase_hits(sig.get("bilingual_bonus",[]),text); job.matched_evidence=list(a["matches"])
    job.score_reasons=[f"family={family}",f"relevance={rel:.1f}",f"qualification={q:.1f}",f"landing-fit={job.landing_score:.1f}",f"career={job.career_score:.1f}",f"remote-confidence={job.remote_confidence:.0f}",f"urgency={job.urgency_score:.0f}",f"application-friction={job.application_friction_score:.0f}"]
    job.score_reasons.append(f"employment={job.employment_class}: {job.employment_reason}")
    job.score_reasons.append(f"source-verification={job.source_verification}: {job.source_verification_reason}")
    if job.work_auth_gate!="unknown": job.score_reasons.append(f"work-auth={job.work_auth_gate}: {job.work_authorization_requirement[:180]}")
    if job.travel_percent is not None: job.score_reasons.append(f"travel={job.travel_percent:.0f}%")
    if job.timezone_requirement: job.score_reasons.append("schedule: "+job.timezone_requirement)
    if a["matches"]: job.score_reasons.append("evidence: "+", ".join(a["matches"][:5]))
    if a["learnable_gaps"]: job.score_reasons.append("gaps: "+", ".join(a["learnable_gaps"][:5]))
    if critical: job.score_reasons.append("critical gaps: "+", ".join(critical[:3]))
    if soft_hits: job.score_reasons.append("soft penalties: "+", ".join(soft_hits[:5]))
    return job

# Make browser capture in the core use the new scoring engine.
c.score_job=score_job
c.pick_profile=pick_profile


def source_snapshot(job: Job) -> dict[str,Any]:
    """Employer/source facts only. Strategy scores are intentionally excluded.

    This allows the ledger to distinguish an employer posting change from a local
    scoring/configuration change.
    """
    return {
        "title": clean_text(job.title),
        "company": clean_text(job.company),
        "location_raw": clean_text(job.location_raw),
        "canonical_url": canonical_url(job.canonical_url),
        "apply_url": canonical_url(job.apply_url),
        "remote_status": clean_text(job.remote_status),
        "employment_type": clean_text(job.employment_type),
        "salary_text": clean_text(job.salary_text),
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "salary_currency": clean_text(job.salary_currency),
        "salary_period": clean_text(job.salary_period),
        "posted_at": clean_text(job.posted_at),
        "description": strip_html(job.description),
        "category": clean_text(job.category),
        "tags": [clean_text(x) for x in (job.tags or []) if clean_text(x)],
        "application_deadline": clean_text(getattr(job,"application_deadline","")),
        "posting_status": clean_text(getattr(job,"posting_status","")),
        "travel_percent": getattr(job,"travel_percent",None),
        "timezone_requirement": clean_text(getattr(job,"timezone_requirement","")),
        "work_authorization_requirement": clean_text(getattr(job,"work_authorization_requirement","")),
    }


def snapshot_hash(snapshot: dict[str,Any]) -> str:
    raw=json.dumps(snapshot,sort_keys=True,ensure_ascii=False,separators=(",",":"))
    return hashlib.sha256(raw.encode("utf-8",errors="ignore")).hexdigest()


def _sentence_chunks(text:str, limit:int=500)->list[str]:
    chunks=[clean_text(x) for x in re.split(r"(?<=[.!?])\s+|\n+", text or "")]
    return [x for x in chunks if len(x)>=8][:limit]


def diff_snapshots(old:dict[str,Any], new:dict[str,Any])->dict[str,Any]:
    diff:dict[str,Any]={}
    simple=["title","company","location_raw","canonical_url","apply_url","remote_status","employment_type","salary_text","salary_min","salary_max","salary_currency","salary_period","posted_at","category","tags","application_deadline","posting_status","travel_percent","timezone_requirement","work_authorization_requirement"]
    for k in simple:
        if old.get(k)!=new.get(k): diff[k]={"old":old.get(k),"new":new.get(k)}
    if old.get("description","")!=new.get("description",""):
        a=_sentence_chunks(old.get("description", "")); b=_sentence_chunks(new.get("description", ""))
        sm=difflib.SequenceMatcher(a=a,b=b,autojunk=False)
        added=[]; removed=[]
        for tag,i1,i2,j1,j2 in sm.get_opcodes():
            if tag in {"insert","replace"}: added.extend(b[j1:j2])
            if tag in {"delete","replace"}: removed.extend(a[i1:i2])
        diff["description"]={
            "old_chars":len(old.get("description", "")),"new_chars":len(new.get("description", "")),
            "added":added[:20],"removed":removed[:20],
        }
    return diff


def occurrence_key(job:Job)->str:
    u=canonical_url(job.canonical_url or job.apply_url)
    if job.source_job_id:
        locator="sid:"+clean_text(job.source_job_id)
    elif u and is_job_specific_url(u):
        locator="url:"+u
    else:
        locator="fallback:"+"|".join([norm(job.company),norm(job.title),remote_identity_bucket(job.location_raw),u])
    raw=f"{job.source_site}|{locator}"
    return "O"+hashlib.sha256(raw.encode("utf-8",errors="ignore")).hexdigest()[:18].upper()



def is_job_specific_url(url:str)->bool:
    try:
        u=urllib.parse.urlsplit(url); host=(u.hostname or "").lower(); path=urllib.parse.unquote(u.path or ""); q=urllib.parse.parse_qs(u.query)
        if any(x in host for x in ("greenhouse.io","lever.co","ashbyhq.com")) and len([x for x in path.split("/") if x])>=2: return True
        if any(k.lower() in {"job","jobid","job_id","jid","gh_jid","requisition","requisitionid","posting"} for k in q): return True
        if re.search(r"(?:^|[/_-])(?:job|jobs|careers|positions?|requisitions?)[/_-].*(?:\d{3,}|[0-9a-f]{8}-[0-9a-f-]{20,})",path,flags=re.I): return True
        if re.search(r"\d{5,}|[0-9a-f]{8}-[0-9a-f-]{20,}",path,flags=re.I) and len(path)>8: return True
    except Exception: pass
    return False


def remote_identity_bucket(location:str)->str:
    loc=norm(location)
    return "remote-us" if (not loc or any(phrase_present(x,loc) for x in ("remote","usa","united states","anywhere"))) else loc


def job_from_row(row:sqlite3.Row)->Job:
    def arr(k:str)->list[str]:
        try:
            x=json.loads(row[k] or "[]"); return x if isinstance(x,list) else []
        except Exception: return []
    j=Job(
        source_site=clean_text(row["canonical_source_site"] if "canonical_source_site" in row.keys() else "ledger") or "ledger",
        source_job_id="", canonical_url=clean_text(row["canonical_url"]), apply_url=clean_text(row["apply_url"]),
        title=clean_text(row["title"]), company=clean_text(row["company"]), location_raw=clean_text(row["location_raw"]),
        remote_status=clean_text(row["remote_status"]), employment_type=clean_text(row["employment_type"]),
        salary_text=clean_text(row["salary_text"]), salary_min=row["salary_min"], salary_max=row["salary_max"],
        salary_currency=clean_text(row["salary_currency"]), salary_period=clean_text(row["salary_period"]),
        posted_at=clean_text(row["posted_at"]), description=clean_text(row["description"]), category=clean_text(row["category"]),
        tags=arr("tags_json"), raw={"rescore_from_ledger":True,"_ledger_canonical_verified":int(row["canonical_verified"] or 0) if "canonical_verified" in row.keys() else 0,"_ledger_source_verification":clean_text(row["source_verification"]) if "source_verification" in row.keys() else "","_ledger_source_verification_reason":clean_text(row["source_verification_reason"]) if "source_verification_reason" in row.keys() else ""},
    )
    return j


class PrecisionStore(c.Store):
    """Append-oriented canonical job ledger.

    Invariants:
      * every source sighting is retained in occurrences;
      * one canonical job can have many source occurrences;
      * unchanged sightings never create a new version;
      * meaningful source changes create immutable versions + field diffs;
      * lower-authority mirrors do not overwrite a higher-authority canonical copy;
      * all jobs, including out-of-scope jobs, stay in the ledger.
    """
    EXTRA={
        "normalized_title_family":"TEXT","relevance_score":"REAL","qualification_score":"REAL","domain_score":"REAL",
        "remote_confidence":"REAL","source_confidence":"REAL","extraction_confidence":"REAL",
        "requirement_matches_json":"TEXT","requirement_gaps_json":"TEXT","required_skills_json":"TEXT","management_required":"INTEGER",
        "posting_status":"TEXT","application_deadline":"TEXT","applied_at":"TEXT","screen_at":"TEXT","interview_at":"TEXT","final_interview_at":"TEXT","offer_at":"TEXT","rejected_at":"TEXT",
        "current_content_hash":"TEXT","canonical_source_site":"TEXT","canonical_source_confidence":"REAL","canonical_occurrence_key":"TEXT",
        "last_changed_at":"TEXT","change_status":"TEXT","update_count":"INTEGER DEFAULT 0",
        "is_active":"INTEGER DEFAULT 1","closed_at":"TEXT","last_scored_at":"TEXT","strategy_version":"TEXT",
        "change_ack_at":"TEXT",
        "travel_percent":"REAL","timezone_requirement":"TEXT","work_auth_gate":"TEXT","work_authorization_requirement":"TEXT",
        "salary_annual_mid":"REAL","application_friction_score":"REAL","urgency_score":"REAL","application_priority_score":"REAL","eligibility_confidence":"REAL",
        "employment_class":"TEXT","employment_reason":"TEXT",
        "source_verification":"TEXT","source_verification_reason":"TEXT","canonical_verified":"INTEGER DEFAULT 0",
        "recall_reason":"TEXT"
    }
    OCC_EXTRA={
        "source_board":"TEXT","content_hash":"TEXT","is_active":"INTEGER DEFAULT 1",
        "missed_complete_scans":"INTEGER DEFAULT 0","last_seen_run_id":"INTEGER"
    }
    RUN_EXTRA={
        "raw_jobs":"INTEGER DEFAULT 0","canonical_seen":"INTEGER DEFAULT 0","unchanged_jobs":"INTEGER DEFAULT 0","closed_jobs":"INTEGER DEFAULT 0"
    }
    def _schema(self):
        super()._schema()
        cols={r[1] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for name,typ in self.EXTRA.items():
            if name not in cols: self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {typ}")
        ocols={r[1] for r in self.conn.execute("PRAGMA table_info(occurrences)")}
        for name,typ in self.OCC_EXTRA.items():
            if name not in ocols: self.conn.execute(f"ALTER TABLE occurrences ADD COLUMN {name} {typ}")
        rcols={r[1] for r in self.conn.execute("PRAGMA table_info(runs)")}
        for name,typ in self.RUN_EXTRA.items():
            if name not in rcols: self.conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {typ}")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS application_events(
          event_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
          event_type TEXT NOT NULL,event_at TEXT NOT NULL,notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS job_versions(
          version_id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL, version_no INTEGER NOT NULL, observed_at TEXT NOT NULL,
          source_site TEXT, occurrence_key TEXT, content_hash TEXT NOT NULL,
          snapshot_json TEXT NOT NULL, diff_json TEXT NOT NULL, reason TEXT NOT NULL,
          UNIQUE(job_id,version_no)
        );
        CREATE INDEX IF NOT EXISTS idx_versions_job ON job_versions(job_id,version_no DESC);
        CREATE TABLE IF NOT EXISTS coverage_segments(
          segment_id TEXT PRIMARY KEY, platform TEXT NOT NULL, mode TEXT NOT NULL,
          search_profile TEXT NOT NULL, query_text TEXT NOT NULL, window_days INTEGER NOT NULL,
          remote_required INTEGER NOT NULL DEFAULT 1, search_url TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'due', last_opened_at TEXT, last_completed_at TEXT,
          completed_count INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_coverage_due ON coverage_segments(platform,status,last_completed_at);
        CREATE TABLE IF NOT EXISTS coverage_events(
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,segment_id TEXT NOT NULL,event_type TEXT NOT NULL,
          event_at TEXT NOT NULL,notes TEXT NOT NULL DEFAULT ''
        );
        """)
        self.conn.commit()
        self._backfill_legacy_versions()

    def _backfill_legacy_versions(self)->None:
        rows=self.conn.execute("SELECT * FROM jobs WHERE current_content_hash IS NULL OR current_content_hash='' OR NOT EXISTS (SELECT 1 FROM job_versions v WHERE v.job_id=jobs.job_id)").fetchall()
        for r in rows:
            snap=self._current_snapshot(r); h=snapshot_hash(snap); now=clean_text(r["last_seen"]) or now_iso()
            self.conn.execute("UPDATE jobs SET current_content_hash=?,canonical_source_site=COALESCE(NULLIF(canonical_source_site,''),'legacy'),canonical_source_confidence=COALESCE(canonical_source_confidence,0),last_changed_at=COALESCE(NULLIF(last_changed_at,''),first_seen),change_status=COALESCE(NULLIF(change_status,''),'MIGRATED'),is_active=COALESCE(is_active,1),last_scored_at=COALESCE(last_scored_at,last_seen),strategy_version=COALESCE(NULLIF(strategy_version,''),'legacy') WHERE job_id=?",(h,r["job_id"]))
            n=self.conn.execute("SELECT COUNT(*) n FROM job_versions WHERE job_id=?",(r["job_id"],)).fetchone()["n"]
            if not n:
                self.conn.execute("INSERT INTO job_versions(job_id,version_no,observed_at,source_site,occurrence_key,content_hash,snapshot_json,diff_json,reason) VALUES (?,?,?,?,?,?,?,?,?)",(r["job_id"],1,now,"legacy","",h,json.dumps(snap,ensure_ascii=False),json.dumps({"type":"migration"}),"migrated_from_pre_v2_ledger"))
        if rows: self.conn.commit()

    def begin_run(self,started:str,mode:str)->int:
        cur=self.conn.execute("INSERT INTO runs(started_at,finished_at,mode,source_status_json,new_jobs,updated_jobs,raw_jobs,canonical_seen,unchanged_jobs,closed_jobs) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (started,"",mode,"{}",0,0,0,0,0,0))
        self.conn.commit(); return int(cur.lastrowid)

    def resolve_job_id(self,job:Job)->str:
        ok=occurrence_key(job)
        r=self.conn.execute("SELECT job_id FROM occurrences WHERE occurrence_key=?",(ok,)).fetchone()
        if r: return clean_text(r["job_id"])
        urls=[canonical_url(x) for x in (job.apply_url,job.canonical_url) if canonical_url(x)]
        for u in urls:
            if not is_job_specific_url(u): continue
            r=self.conn.execute("SELECT job_id FROM jobs WHERE canonical_url=? OR apply_url=? LIMIT 1",(u,u)).fetchone()
            if r: return clean_text(r["job_id"])
            r=self.conn.execute("SELECT job_id FROM occurrences WHERE source_url=? OR apply_url=? LIMIT 1",(u,u)).fetchone()
            if r: return clean_text(r["job_id"])
        # Strong exact cross-source fingerprint: same company + title + essentially identical description.
        if job.company and job.title and len(job.description or "")>500:
            dh=hashlib.sha256(norm(job.description)[:12000].encode()).hexdigest()[:20]
            candidates=self.conn.execute("SELECT job_id,description FROM jobs WHERE lower(company)=lower(?) AND lower(title)=lower(?) LIMIT 20",(job.company,job.title)).fetchall()
            for x in candidates:
                if x["description"] and hashlib.sha256(norm(x["description"])[:12000].encode()).hexdigest()[:20]==dh:
                    return clean_text(x["job_id"])
        return job.job_id

    def _current_snapshot(self,row:sqlite3.Row)->dict[str,Any]:
        try: tags=json.loads(row["tags_json"] or "[]")
        except Exception: tags=[]
        return {
          "title":clean_text(row["title"]),"company":clean_text(row["company"]),"location_raw":clean_text(row["location_raw"]),
          "canonical_url":canonical_url(row["canonical_url"]),"apply_url":canonical_url(row["apply_url"]),
          "remote_status":clean_text(row["remote_status"]),"employment_type":clean_text(row["employment_type"]),
          "salary_text":clean_text(row["salary_text"]),"salary_min":row["salary_min"],"salary_max":row["salary_max"],
          "salary_currency":clean_text(row["salary_currency"]),"salary_period":clean_text(row["salary_period"]),
          "posted_at":clean_text(row["posted_at"]),"description":strip_html(row["description"]),"category":clean_text(row["category"]),"tags":tags,
          "application_deadline":clean_text(row["application_deadline"]) if "application_deadline" in row.keys() else "",
          "posting_status":clean_text(row["posting_status"]) if "posting_status" in row.keys() else "",
          "travel_percent":row["travel_percent"] if "travel_percent" in row.keys() else None,
          "timezone_requirement":clean_text(row["timezone_requirement"]) if "timezone_requirement" in row.keys() else "",
          "work_authorization_requirement":clean_text(row["work_authorization_requirement"]) if "work_authorization_requirement" in row.keys() else "",
        }

    def _strategy_values(self,job:Job)->dict[str,Any]:
        return {
            "search_profile":job.search_profile,"career_lane":job.career_lane,"resume_variant":job.resume_variant,
            "matched_keywords_json":json.dumps(job.matched_keywords,ensure_ascii=False),"remote_gate":job.remote_gate,
            "remote_gate_reason":job.remote_gate_reason,"hard_reject_reasons_json":json.dumps(job.hard_reject_reasons,ensure_ascii=False),
            "matched_evidence_json":json.dumps(job.matched_evidence,ensure_ascii=False),"matched_positive_json":json.dumps(job.matched_positive,ensure_ascii=False),
            "matched_accelerators_json":json.dumps(job.matched_accelerators,ensure_ascii=False),"matched_bilingual_json":json.dumps(job.matched_bilingual,ensure_ascii=False),
            "years_required":job.years_required,"landing_score":job.landing_score,"career_score":job.career_score,"door_score":job.door_score,
            "recommendation":job.recommendation,"score_reasons_json":json.dumps(job.score_reasons,ensure_ascii=False),
            "normalized_title_family":clean_text(getattr(job,"normalized_title_family","")),"relevance_score":float(getattr(job,"relevance_score",0)),
            "qualification_score":float(getattr(job,"qualification_score",0)),"domain_score":float(getattr(job,"domain_score",0)),
            "remote_confidence":float(getattr(job,"remote_confidence",0)),"source_confidence":float(getattr(job,"source_confidence",0)),
            "extraction_confidence":float(getattr(job,"extraction_confidence",0)),
            "requirement_matches_json":json.dumps(getattr(job,"requirement_matches",[]),ensure_ascii=False),
            "requirement_gaps_json":json.dumps(getattr(job,"requirement_gaps",[]),ensure_ascii=False),
            "required_skills_json":json.dumps(getattr(job,"required_skills",[]),ensure_ascii=False),
            "management_required":int(getattr(job,"management_required",0)),"posting_status":clean_text(getattr(job,"posting_status","unknown")),
            "application_deadline":clean_text(getattr(job,"application_deadline","")),"last_scored_at":now_iso(),"strategy_version":VERSION,
            "travel_percent":getattr(job,"travel_percent",None),"timezone_requirement":clean_text(getattr(job,"timezone_requirement","")),
            "work_auth_gate":clean_text(getattr(job,"work_auth_gate","unknown")),"work_authorization_requirement":clean_text(getattr(job,"work_authorization_requirement","")),
            "salary_annual_mid":getattr(job,"salary_annual_mid",None),"application_friction_score":float(getattr(job,"application_friction_score",0)),
            "urgency_score":float(getattr(job,"urgency_score",0)),"application_priority_score":float(getattr(job,"application_priority_score",0)),
            "eligibility_confidence":float(getattr(job,"eligibility_confidence",0)),
            "employment_class":clean_text(getattr(job,"employment_class","unknown")),
            "employment_reason":clean_text(getattr(job,"employment_reason","")),
            "source_verification":clean_text(getattr(job,"source_verification","unverified_discovery")),
            "source_verification_reason":clean_text(getattr(job,"source_verification_reason","")),
            "canonical_verified":int(getattr(job,"canonical_verified",0) or 0),
            "recall_reason":clean_text(getattr(job,"recall_reason","")),
        }

    def _insert_version(self,jid:str,snap:dict[str,Any],h:str,job:Job,ok:str,diff:dict[str,Any],reason:str):
        n=int(self.conn.execute("SELECT COALESCE(MAX(version_no),0)+1 n FROM job_versions WHERE job_id=?",(jid,)).fetchone()["n"])
        self.conn.execute("INSERT INTO job_versions(job_id,version_no,observed_at,source_site,occurrence_key,content_hash,snapshot_json,diff_json,reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (jid,n,now_iso(),job.source_site,ok,h,json.dumps(snap,ensure_ascii=False),json.dumps(diff,ensure_ascii=False),reason))

    def upsert(self,job:Job,run_id:Optional[int]=None,commit:bool=True)->str:
        now=now_iso(); jid=self.resolve_job_id(job); ok=occurrence_key(job); snap=source_snapshot(job); h=snapshot_hash(snap)
        board=clean_text((job.raw or {}).get("_board") or (job.raw or {}).get("board") or "")
        existing=self.conn.execute("SELECT * FROM jobs WHERE job_id=?",(jid,)).fetchone()
        incoming_conf=float(getattr(job,"source_confidence",0) or 0)
        status="new"
        if existing is None:
            vals={
                "job_id":jid,**{k:snap[k] for k in ("title","company","location_raw","canonical_url","apply_url","remote_status","employment_type","salary_text","salary_min","salary_max","salary_currency","salary_period","posted_at","description","category")},
                "tags_json":json.dumps(snap["tags"],ensure_ascii=False),**self._strategy_values(job),
                "first_seen":now,"last_seen":now,"current_content_hash":h,"canonical_source_site":job.source_site,
                "canonical_source_confidence":incoming_conf,"canonical_occurrence_key":ok,"last_changed_at":now,"change_status":"NEW","update_count":0,
                "is_active":0 if clean_text(getattr(job,"posting_status","unknown"))=="closed" else 1,
                "closed_at":now if clean_text(getattr(job,"posting_status","unknown"))=="closed" else None,
            }
            cols=list(vals); self.conn.execute(f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",[vals[x] for x in cols])
            self._insert_version(jid,snap,h,job,ok,{"type":"new"},"new")
        else:
            oldsnap=self._current_snapshot(existing); oldh=clean_text(existing["current_content_hash"]) or snapshot_hash(oldsnap)
            old_conf=float(existing["canonical_source_confidence"] or 0)
            same_canonical_occurrence=clean_text(existing["canonical_occurrence_key"])==ok
            authoritative=same_canonical_occurrence or incoming_conf>old_conf or not clean_text(existing["description"])
            sv=self._strategy_values(job)
            # Strategy fields are refreshed only from an authoritative/current source; a low-quality mirror cannot degrade the canonical score.
            if authoritative:
                sets=[]; args=[]
                for k,v in sv.items(): sets.append(f"{k}=?"); args.append(v)
                args.extend([now,jid]); self.conn.execute(f"UPDATE jobs SET {','.join(sets)},last_seen=? WHERE job_id=?",args)
            else:
                self.conn.execute("UPDATE jobs SET last_seen=? WHERE job_id=?",(now,jid))
            reopened=bool(existing["is_active"]==0 and clean_text(getattr(job,"posting_status","unknown"))!="closed" and authoritative)
            if authoritative and (h!=oldh or reopened):
                d=diff_snapshots(oldsnap,snap)
                if reopened: d["posting_status"]={"old":"closed","new":clean_text(getattr(job,"posting_status","unknown")) or "active"}
                sets=[]; args=[]
                for k in ("title","company","location_raw","canonical_url","apply_url","remote_status","employment_type","salary_text","salary_min","salary_max","salary_currency","salary_period","posted_at","description","category"):
                    sets.append(f"{k}=?"); args.append(snap[k])
                sets.append("tags_json=?"); args.append(json.dumps(snap["tags"],ensure_ascii=False))
                sets += ["current_content_hash=?","canonical_source_site=?","canonical_source_confidence=?","canonical_occurrence_key=?","last_changed_at=?","change_status='UPDATED'","update_count=COALESCE(update_count,0)+1","is_active=?","closed_at=?"]
                args += [h,job.source_site,incoming_conf,ok,now,0 if clean_text(getattr(job,"posting_status","unknown"))=="closed" else 1, now if clean_text(getattr(job,"posting_status","unknown"))=="closed" else None,jid]
                self.conn.execute(f"UPDATE jobs SET {','.join(sets)} WHERE job_id=?",args)
                self._insert_version(jid,snap,h,job,ok,d,"reopened" if reopened else "updated")
                status="updated"
            else:
                status="unchanged"
        # Every source encounter is logged independently.
        occ=self.conn.execute("SELECT * FROM occurrences WHERE occurrence_key=?",(ok,)).fetchone()
        if occ:
            self.conn.execute("UPDATE occurrences SET job_id=?,source_url=?,apply_url=?,raw_json=?,last_seen=?,seen_count=seen_count+1,source_board=?,content_hash=?,is_active=1,missed_complete_scans=0,last_seen_run_id=? WHERE occurrence_key=?",
                (jid,job.canonical_url,job.apply_url,json.dumps(job.raw,ensure_ascii=False),now,board,h,run_id,ok))
        else:
            self.conn.execute("INSERT INTO occurrences(occurrence_key,job_id,source_site,source_job_id,source_url,apply_url,raw_json,first_seen,last_seen,seen_count,source_board,content_hash,is_active,missed_complete_scans,last_seen_run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ok,jid,job.source_site,job.source_job_id,job.canonical_url,job.apply_url,json.dumps(job.raw,ensure_ascii=False),now,now,1,board,h,1,0,run_id))
        self.conn.execute("UPDATE jobs SET seen_count=seen_count+? WHERE job_id=?",(0 if existing is None else 1,jid))
        if commit: self.conn.commit()
        return status

    def update_scores_only(self,jid:str,job:Job,commit:bool=True)->None:
        sv=self._strategy_values(job); sets=",".join(f"{k}=?" for k in sv); self.conn.execute(f"UPDATE jobs SET {sets} WHERE job_id=?",[sv[k] for k in sv]+[jid])
        if commit: self.conn.commit()

    def rescore_all(self,strategy:dict[str,Any],candidate:dict[str,Any],mode:str="deep")->dict[str,int]:
        counts={}
        rows=self.conn.execute("SELECT * FROM jobs").fetchall()
        # One transaction, not one fsync/commit per job. This keeps 50k+ ledgers practical.
        self.conn.execute("BEGIN")
        try:
            for r in rows:
                j=job_from_row(r); setattr(j,"_mode",mode); score_job(j,strategy,candidate); self.update_scores_only(r["job_id"],j,commit=False); counts[j.recommendation]=counts.get(j.recommendation,0)+1
            self.conn.commit()
        except Exception:
            self.conn.rollback(); raise
        return counts

    def reconcile_complete_board(self,source_site:str,board:str,seen_source_ids:set[str],run_id:int,miss_threshold:int=2)->int:
        """Close missing jobs only after repeated *complete* public ATS scans."""
        if not board: return 0
        rows=self.conn.execute("SELECT * FROM occurrences WHERE source_site=? AND source_board=? AND is_active=1",(source_site,board)).fetchall(); closed=0
        for o in rows:
            sid=clean_text(o["source_job_id"])
            if sid and sid in seen_source_ids:
                self.conn.execute("UPDATE occurrences SET missed_complete_scans=0,last_seen_run_id=? WHERE occurrence_key=?",(run_id,o["occurrence_key"])); continue
            missed=int(o["missed_complete_scans"] or 0)+1
            self.conn.execute("UPDATE occurrences SET missed_complete_scans=? WHERE occurrence_key=?",(missed,o["occurrence_key"]))
            if missed<miss_threshold: continue
            self.conn.execute("UPDATE occurrences SET is_active=0 WHERE occurrence_key=?",(o["occurrence_key"],))
            jr=self.conn.execute("SELECT * FROM jobs WHERE job_id=?",(o["job_id"],)).fetchone()
            if not jr or jr["is_active"]==0: continue
            # A complete canonical ATS disappearance is authoritative for closure.
            if clean_text(jr["canonical_source_site"])==source_site:
                now=now_iso(); oldsnap=self._current_snapshot(jr); ch=clean_text(jr["current_content_hash"]) or snapshot_hash(oldsnap)
                self.conn.execute("UPDATE jobs SET is_active=0,posting_status='closed',closed_at=?,last_changed_at=?,change_status='UPDATED',update_count=COALESCE(update_count,0)+1 WHERE job_id=?",(now,now,o["job_id"]))
                n=int(self.conn.execute("SELECT COALESCE(MAX(version_no),0)+1 n FROM job_versions WHERE job_id=?",(o["job_id"],)).fetchone()["n"])
                self.conn.execute("INSERT INTO job_versions(job_id,version_no,observed_at,source_site,occurrence_key,content_hash,snapshot_json,diff_json,reason) VALUES (?,?,?,?,?,?,?,?,?)",
                    (o["job_id"],n,now,source_site,o["occurrence_key"],ch,json.dumps(oldsnap,ensure_ascii=False),json.dumps({"posting_status":{"old":jr["posting_status"] or "active","new":"closed"}},ensure_ascii=False),"closed_after_missing_complete_ats_scans")); closed+=1
        self.conn.commit(); return closed

    def rows(self,active_only:bool=False)->list[sqlite3.Row]:
        q="SELECT * FROM jobs"+(" WHERE is_active=1" if active_only else "")+" ORDER BY door_score DESC,last_seen DESC"
        return list(self.conn.execute(q))

    def versions(self,job_id:str)->list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM job_versions WHERE job_id=? ORDER BY version_no DESC",(job_id,)))

    def mark(self,job_id:str,status:str,notes:str=""):
        super().mark(job_id,status,notes); col={"applied":"applied_at","screen":"screen_at","interview":"interview_at","final_interview":"final_interview_at","offer":"offer_at","rejected":"rejected_at"}.get(status.lower())
        if col: self.conn.execute(f"UPDATE jobs SET {col}=COALESCE({col},?) WHERE job_id=?",(now_iso(),job_id))
        self.conn.execute("INSERT INTO application_events(job_id,event_type,event_at,notes) VALUES (?,?,?,?)",(job_id,status,now_iso(),notes)); self.conn.commit()

    def record_run(self,run_id:int,status:dict[str,Any],raw_jobs:int,canonical_seen:int,new:int,updated:int,unchanged:int,closed:int):
        self.conn.execute("UPDATE runs SET finished_at=?,source_status_json=?,new_jobs=?,updated_jobs=?,raw_jobs=?,canonical_seen=?,unchanged_jobs=?,closed_jobs=? WHERE run_id=?",
            (now_iso(),json.dumps(status,ensure_ascii=False),new,updated,raw_jobs,canonical_seen,unchanged,closed,run_id)); self.conn.commit()

    def acknowledge_changes(self)->int:
        cur=self.conn.execute("UPDATE jobs SET change_ack_at=? WHERE change_status IN ('NEW','UPDATED') AND (change_ack_at IS NULL OR change_ack_at<last_changed_at)",(now_iso(),)); self.conn.commit(); return cur.rowcount

    def sync_coverage_segments(self,segments:list[dict[str,Any]])->None:
        for x in segments:
            generated_note=clean_text(x.get("notes") or "")
            self.conn.execute("""INSERT INTO coverage_segments(segment_id,platform,mode,search_profile,query_text,window_days,remote_required,search_url,status,notes)
                VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(segment_id) DO UPDATE SET search_url=excluded.search_url,query_text=excluded.query_text,mode=excluded.mode,search_profile=excluded.search_profile,window_days=excluded.window_days,notes=CASE WHEN excluded.notes='' THEN coverage_segments.notes WHEN instr(coverage_segments.notes,excluded.notes)>0 THEN coverage_segments.notes WHEN coverage_segments.notes='' THEN excluded.notes ELSE excluded.notes||' | '||coverage_segments.notes END""",
                (x["segment_id"],x["platform"],x["mode"],x["search_profile"],x["query_text"],x["window_days"],1,x["search_url"],"due",generated_note))
        self.conn.commit()
        # Recurring coverage: 24h/7d segments are due again every day; 30d backfill weekly.
        for r in self.conn.execute("SELECT segment_id,window_days,last_completed_at,status FROM coverage_segments").fetchall():
            last=parse_dt(r["last_completed_at"]) if r["last_completed_at"] else None
            cadence=12 if int(r["window_days"] or 1)<=1 else 20 if int(r["window_days"] or 1)<=7 else 144
            if clean_text(r["status"])=="superseded": continue
            if last is None or (utcnow()-last).total_seconds()/3600>=cadence:
                self.conn.execute("UPDATE coverage_segments SET status='due' WHERE segment_id=?",(r["segment_id"],))
        self.conn.commit()

    def coverage_rows(self,platform:str="",due_only:bool=False)->list[sqlite3.Row]:
        cond=[]; args=[]
        if platform: cond.append("platform=?"); args.append(platform)
        if due_only: cond.append("status='due'")
        q="SELECT * FROM coverage_segments"+(" WHERE "+" AND ".join(cond) if cond else "")+" ORDER BY platform,window_days,search_profile,query_text"
        return list(self.conn.execute(q,args))

    def split_coverage_segment(self,segment_id:str)->list[str]:
        r=self.conn.execute("SELECT * FROM coverage_segments WHERE segment_id=?",(segment_id,)).fetchone()
        if not r: raise ValueError(f"Unknown coverage segment: {segment_id}")
        terms=re.findall(r'"([^"]+)"',r["query_text"] or "")
        if len(terms)<=1: raise ValueError("Segment cannot be split further; it contains one term")
        mid=(len(terms)+1)//2; ids=[]
        for chunk in (terms[:mid],terms[mid:]):
            if not chunk: continue
            q="("+" OR ".join('"'+x+'"' for x in chunk)+")"
            raw=f"{r['platform']}|{r['mode']}|{r['search_profile']}|{r['window_days']}|{q}"; sid="S"+hashlib.sha256(raw.encode()).hexdigest()[:16].upper(); url=platform_search_url(r["platform"],q,int(r["window_days"]))
            self.conn.execute("INSERT OR IGNORE INTO coverage_segments(segment_id,platform,mode,search_profile,query_text,window_days,remote_required,search_url,status) VALUES (?,?,?,?,?,?,?,?,?)",(sid,r["platform"],r["mode"],r["search_profile"],q,r["window_days"],r["remote_required"],url,"due")); ids.append(sid)
        self.conn.execute("UPDATE coverage_segments SET status='superseded',notes=trim(notes||' split because result cap/coverage breadth required smaller query') WHERE segment_id=?",(segment_id,)); self.conn.execute("INSERT INTO coverage_events(segment_id,event_type,event_at,notes) VALUES (?,?,?,?)",(segment_id,"split",now_iso(),",".join(ids))); self.conn.commit(); return ids

    def coverage_opened(self,segment_id:str)->None:
        now=now_iso(); self.conn.execute("UPDATE coverage_segments SET last_opened_at=? WHERE segment_id=?",(now,segment_id)); self.conn.execute("INSERT INTO coverage_events(segment_id,event_type,event_at) VALUES (?,?,?)",(segment_id,"opened",now)); self.conn.commit()

    def coverage_done(self,segment_id:str,notes:str="")->None:
        now=now_iso(); self.conn.execute("UPDATE coverage_segments SET status='complete',last_completed_at=?,completed_count=completed_count+1,notes=CASE WHEN ?='' THEN notes ELSE ? END WHERE segment_id=?",(now,notes,notes,segment_id)); self.conn.execute("INSERT INTO coverage_events(segment_id,event_type,event_at,notes) VALUES (?,?,?,?)",(segment_id,"completed",now,notes)); self.conn.commit()


def jsoncol(row: sqlite3.Row,key:str)->list[Any]:
    try: return json.loads(row[key] or "[]")
    except Exception: return []


def select_daily_plan(rows:list[sqlite3.Row],strategy:dict[str,Any],target:Optional[int]=None,empirical_boosts:Optional[dict[str,float]]=None)->list[sqlite3.Row]:
    tc=strategy.get("strategy",{}).get("throughput",{}); target=int(target or tc.get("daily_target",15)); target=max(int(tc.get("daily_minimum",10)),min(int(tc.get("daily_maximum",20)),target))
    done={"applied","screen","interview","final_interview","offer","rejected","withdrawn"}
    elig=[r for r in rows if int(r["is_active"] or 0)==1 and clean_text(r["posting_status"])!="closed" and r["application_status"] not in done and r["recommendation"] in {"APPLY_NOW","APPLY_VOLUME","HIGH_VALUE_STRETCH"}]
    rank={"APPLY_NOW":0,"APPLY_VOLUME":1,"HIGH_VALUE_STRETCH":2}; empirical_boosts=empirical_boosts or {}; elig.sort(key=lambda r:(rank.get(r["recommendation"],9),-((r["application_priority_score"] or 0)+empirical_boosts.get(norm(r["normalized_title_family"] or r["title"]),0.0)),-(r["door_score"] or 0),-(r["qualification_score"] or 0),-(r["relevance_score"] or 0)))
    out=[]; selected=set(); cc={}; fc={}; maxc=int(tc.get("max_same_company_per_day",2)); maxf=int(tc.get("max_same_title_family_per_day",4))
    # Pass 1: maximize diversity while keeping the strongest recommendations first.
    for r in elig:
        company=norm(r["company"]); family=norm(r["normalized_title_family"] or r["title"])
        if cc.get(company,0)>=maxc or fc.get(family,0)>=maxf: continue
        out.append(r); selected.add(r["job_id"]); cc[company]=cc.get(company,0)+1; fc[family]=fc.get(family,0)+1
        if len(out)>=target: return out
    # Pass 2: title-family diversity is a soft preference, not a throughput ceiling.
    # Keep the per-company cap, but fill remaining slots from already-qualified jobs.
    for r in elig:
        if r["job_id"] in selected: continue
        company=norm(r["company"])
        if cc.get(company,0)>=maxc: continue
        out.append(r); selected.add(r["job_id"]); cc[company]=cc.get(company,0)+1
        if len(out)>=target: break
    return out



def funnel_boosts(store:PrecisionStore,strategy:dict[str,Any])->dict[str,float]:
    cfg=strategy.get("strategy",{}).get("feedback",{}); cohort=int(cfg.get("initial_learning_cohort",50)); prior=float(cfg.get("prior_strength",10));
    total=store.conn.execute("SELECT COUNT(*) n,SUM(CASE WHEN screen_at IS NOT NULL OR interview_at IS NOT NULL OR offer_at IS NOT NULL THEN 1 ELSE 0 END) s FROM jobs WHERE applied_at IS NOT NULL").fetchone(); n=int(total["n"] or 0); scr=int(total["s"] or 0)
    if n<cohort: return {}
    overall=scr/n if n else 0.0; out={}
    rows=store.conn.execute("""SELECT normalized_title_family family,COUNT(*) n,SUM(CASE WHEN screen_at IS NOT NULL OR interview_at IS NOT NULL OR offer_at IS NOT NULL THEN 1 ELSE 0 END) s FROM jobs WHERE applied_at IS NOT NULL AND normalized_title_family<>'' GROUP BY normalized_title_family""").fetchall()
    for r in rows:
        fn=int(r["n"] or 0); fs=int(r["s"] or 0); rate=(fs+prior*overall)/(fn+prior); out[norm(r["family"])]=max(-6.0,min(6.0,(rate-overall)*35.0))
    return out


def build_funnel_report(store:PrecisionStore,out:Path,strategy:dict[str,Any])->Path:
    out.mkdir(parents=True,exist_ok=True); total=store.conn.execute("""SELECT COUNT(*) applied,SUM(CASE WHEN screen_at IS NOT NULL THEN 1 ELSE 0 END) screens,SUM(CASE WHEN interview_at IS NOT NULL THEN 1 ELSE 0 END) interviews,SUM(CASE WHEN offer_at IS NOT NULL THEN 1 ELSE 0 END) offers FROM jobs WHERE applied_at IS NOT NULL""").fetchone(); n=int(total["applied"] or 0)
    lines=["# Candidate Funnel Report",f"Generated: {now_iso()}",f"Applied: {n} | Screens: {int(total['screens'] or 0)} | Interviews: {int(total['interviews'] or 0)} | Offers: {int(total['offers'] or 0)}",""]
    for label,col in [("Title family","normalized_title_family"),("Career lane","career_lane"),("Resume variant","resume_variant"),("Canonical source","canonical_source_site")]:
        lines += [f"## {label}","| Segment | Applied | Screen+ | Interview+ | Offer | Screen rate |","|---|---:|---:|---:|---:|---:|"]
        q=f"""SELECT {col} seg,COUNT(*) n,SUM(CASE WHEN screen_at IS NOT NULL OR interview_at IS NOT NULL OR offer_at IS NOT NULL THEN 1 ELSE 0 END) scr,SUM(CASE WHEN interview_at IS NOT NULL OR offer_at IS NOT NULL THEN 1 ELSE 0 END) intr,SUM(CASE WHEN offer_at IS NOT NULL THEN 1 ELSE 0 END) off FROM jobs WHERE applied_at IS NOT NULL GROUP BY {col} ORDER BY n DESC"""
        for r in store.conn.execute(q):
            rn=int(r["n"] or 0); rs=int(r["scr"] or 0); lines.append(f"| {clean_text(r['seg']) or '(unknown)'} | {rn} | {rs} | {int(r['intr'] or 0)} | {int(r['off'] or 0)} | {(rs/rn*100 if rn else 0):.1f}% |")
        lines.append("")
    boosts=funnel_boosts(store,strategy); lines += ["## Active empirical ranking adjustments"]
    if boosts: lines += [f"- {k}: {v:+.2f} daily-priority points" for k,v in sorted(boosts.items(),key=lambda x:-x[1])]
    else: lines += [f"- Not active yet. Strategy requires {int(strategy.get('strategy',{}).get('feedback',{}).get('initial_learning_cohort',50))} applications before candidate-specific reweighting."]
    path=out/"funnel_report.md"; path.write_text("\n".join(lines)+"\n",encoding="utf-8"); return path


def progress_summary(store:PrecisionStore,strategy:dict[str,Any])->str:
    r=store.conn.execute("""SELECT
      SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END) applied,
      SUM(CASE WHEN date(applied_at)=date('now') THEN 1 ELSE 0 END) today,
      SUM(CASE WHEN screen_at IS NOT NULL THEN 1 ELSE 0 END) screens,
      SUM(CASE WHEN interview_at IS NOT NULL THEN 1 ELSE 0 END) interviews,
      SUM(CASE WHEN offer_at IS NOT NULL THEN 1 ELSE 0 END) offers,
      SUM(CASE WHEN is_active=1 AND application_status NOT IN ('applied','screen','interview','final_interview','offer','rejected','withdrawn') AND recommendation IN ('APPLY_NOW','APPLY_VOLUME','HIGH_VALUE_STRETCH') THEN 1 ELSE 0 END) reservoir
      FROM jobs""").fetchone()
    goal=int(strategy.get("strategy",{}).get("throughput",{}).get("cumulative_application_goal",500)); target=int(strategy.get("strategy",{}).get("throughput",{}).get("daily_target",15)); applied=int(r["applied"] or 0); reservoir=int(r["reservoir"] or 0)
    remaining=max(0,goal-applied); days=(remaining+target-1)//target if target else 0; runway=reservoir/target if target else 0
    return f"Applications: {applied}/{goal} cumulative ({applied/goal*100:.1f}%) | today {int(r['today'] or 0)}/{target} | qualified active reservoir {reservoir} (~{runway:.1f} target-days) | screens {int(r['screens'] or 0)} | interviews {int(r['interviews'] or 0)} | offers {int(r['offers'] or 0)} | ~{days} target-days to {goal} at {target}/day"


def build_daily_plan(store:PrecisionStore,out:Path,strategy:dict[str,Any],target:Optional[int]=None)->list[sqlite3.Row]:
    boosts=funnel_boosts(store,strategy); picked=select_daily_plan(store.rows(),strategy,target,boosts); out.mkdir(parents=True,exist_ok=True)
    fields=["job_id","recommendation","application_priority_score","door_score","landing_score","career_score","relevance_score","qualification_score","urgency_score","application_friction_score","title","company","location_raw","salary_text","posted_at","normalized_title_family","career_lane","resume_variant","employment_class","source_verification","canonical_verified","work_auth_gate","travel_percent","timezone_requirement","apply_url","application_status"]
    with (out/"daily_apply_plan.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); [w.writerow({k:csv_safe_cell(r[k]) for k in fields}) for r in picked]
    lines=["# Daily Application Plan",f"Generated: {now_iso()}",f"Jobs selected: {len(picked)}",f"Candidate-specific funnel reweighting: {'active' if boosts else 'not active yet'}",""]
    for i,r in enumerate(picked,1):
        lines += [f"## {i}. {r['title']} — {r['company']}",f"{r['recommendation']} | Priority {r['application_priority_score']:.1f} | Door {r['door_score']:.1f} | Landing-fit {r['landing_score']:.1f} | Qualification {r['qualification_score']:.1f} | Career {r['career_score']:.1f}",f"Resume: {r['resume_variant']} | Remote: {r['remote_gate']} | Work-auth: {r['work_auth_gate']} | Travel: {r['travel_percent'] if r['travel_percent'] is not None else 'unknown'}% | Salary: {r['salary_text'] or 'unknown'}",f"URL: {r['apply_url'] or r['canonical_url']}",f"Evidence: {'; '.join(jsoncol(r,'requirement_matches_json')[:5]) or 'see dashboard'}",f"Gaps: {'; '.join(jsoncol(r,'requirement_gaps_json')[:5]) or 'none detected'}",""]
    (out/"daily_apply_plan.md").write_text("\n".join(lines),encoding="utf-8")
    cards=[]
    for i,r in enumerate(picked,1):
        url=c.safe_output_url(r["apply_url"] or r["canonical_url"])
        cards.append(f"<section><h2>{i}. {c.html.escape(r['title'])}</h2><b>{c.html.escape(r['company'])}</b><p>{r['recommendation']} · Priority {r['application_priority_score']:.1f} · Door {r['door_score']:.1f} · Landing {r['landing_score']:.1f} · Qual {r['qualification_score']:.1f} · Career {r['career_score']:.1f}</p><p>{c.html.escape(r['location_raw'] or '')} · {c.html.escape(r['salary_text'] or 'salary unknown')}</p><p><a target='_blank' rel='noopener noreferrer' referrerpolicy='no-referrer' href='{c.html.escape(url,quote=True)}'>Open application</a> · <code>{r['job_id']}</code></p></section>")
    dh="<!doctype html><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'\"><title>Today\'s Applications</title><style>body{font-family:system-ui;max-width:1000px;margin:28px auto;padding:0 18px;background:#f6f8fb;color:#172033}section{background:white;border:1px solid #e2e7ef;border-left:6px solid #15965a;border-radius:12px;padding:14px;margin:10px 0}h1,h2{margin-top:0}a{color:#1359b2}</style><h1>Today\'s Application Slate</h1><p>Targeted for 10–20 strong applications/day without lowering precision thresholds.</p>"+"".join(cards)
    (out/"daily_apply_plan.html").write_text(dh,encoding="utf-8")
    return picked


def latest_diff(store:PrecisionStore,job_id:str)->dict[str,Any]:
    r=store.conn.execute("SELECT diff_json,reason,observed_at,version_no FROM job_versions WHERE job_id=? ORDER BY version_no DESC LIMIT 1",(job_id,)).fetchone()
    if not r: return {}
    try: d=json.loads(r["diff_json"] or "{}")
    except Exception: d={}
    return {"version_no":r["version_no"],"reason":r["reason"],"observed_at":r["observed_at"],"diff":d}


def build_dashboard(store:PrecisionStore,out:Path):
    rows=store.rows(); fields=["job_id","recommendation","application_priority_score","door_score","landing_score","career_score","relevance_score","qualification_score","urgency_score","application_friction_score","eligibility_confidence","remote_confidence","source_confidence","extraction_confidence","title","company","location_raw","salary_text","posted_at","posting_status","application_deadline","normalized_title_family","career_lane","search_profile","resume_variant","remote_gate","employment_type","travel_percent","timezone_requirement","work_auth_gate","work_authorization_requirement","salary_annual_mid","canonical_url","apply_url","application_status","notes","change_status","last_changed_at","update_count","is_active","canonical_source_site","employment_class","employment_reason","source_verification","source_verification_reason","canonical_verified","recall_reason","first_seen","last_seen"]
    data=[]
    for r in rows:
        d={k:r[k] for k in fields}; d["score_reasons"]=jsoncol(r,"score_reasons_json"); d["hard_reject_reasons"]=jsoncol(r,"hard_reject_reasons_json"); d["requirement_matches"]=jsoncol(r,"requirement_matches_json"); d["requirement_gaps"]=jsoncol(r,"requirement_gaps_json"); d["description"]=(r["description"] or "")[:9000]
        d["source_count"]=int(store.conn.execute("SELECT COUNT(*) n FROM occurrences WHERE job_id=?",(r["job_id"],)).fetchone()["n"] or 0)
        data.append(d)
    js=json.dumps(data,ensure_ascii=False).replace("</","<\\/")
    template="""<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"><meta name="referrer" content="no-referrer"><title>Ultimate Job Ledger</title>
<style>body{font-family:Inter,system-ui,sans-serif;margin:0;background:#f6f8fb;color:#172033}header{padding:18px 24px;background:white;border-bottom:1px solid #e6e9ef;position:sticky;top:0;z-index:5}h1{margin:0 0 4px}.controls{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}input,select{font:inherit;padding:8px 10px;border:1px solid #ccd3df;border-radius:9px;background:white}main{padding:18px 24px}.summary{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}.chip{background:white;border:1px solid #e3e7ef;border-radius:12px;padding:8px 11px}.job{background:white;border:1px solid #e2e7ef;border-radius:14px;padding:15px;margin:9px 0}.top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.scores{white-space:nowrap;font-weight:700}.meta{color:#5a6678;margin:6px 0}.tag{display:inline-block;padding:4px 8px;border-radius:999px;background:#eef2f8;margin:2px;font-size:12px}.NEW{background:#e8f7ee}.UPDATED{background:#fff0dc}.APPLY_NOW{border-left:6px solid #15965a}.APPLY_VOLUME{border-left:6px solid #48a86a}.HIGH_VALUE_STRETCH{border-left:6px solid #6f55d8}.REVIEW,.REVIEW_REMOTE{border-left:6px solid #377bd8}.OUT_OF_SCOPE,.SKIP_HARD_GATE{opacity:.55}.inactive{opacity:.46}.gap{color:#9a3d26}.ok{color:#176b45}details{margin-top:8px}a{color:#1359b2}</style>
<header><h1>Ultimate Job Ledger</h1><div>Exhaustive append-only discovery → canonical dedupe → version/diff → precision qualification → cumulative application reservoir · <a href="retrieval_audit.html">retrieval audit</a> · <a href="updates.html">change feed</a></div><div class="controls"><input id="q" placeholder="Search"><select id="rec"><option value="">All recommendations</option><option>APPLY_NOW</option><option>APPLY_VOLUME</option><option>HIGH_VALUE_STRETCH</option><option>REVIEW</option><option>REVIEW_REMOTE</option><option>VERIFY_SOURCE</option><option>REVIEW_EMPLOYMENT</option><option>CONTRACT_REVIEW</option><option>PART_TIME_REVIEW</option><option>SKIP_SOURCE</option><option>LOW_PRIORITY</option><option>OUT_OF_SCOPE</option><option>SKIP_HARD_GATE</option></select><select id="chg"><option value="">All change states</option><option>NEW</option><option>UPDATED</option></select><select id="active"><option value="1">Active only</option><option value="">Active + closed</option><option value="0">Closed only</option></select><select id="lane"><option value="">All lanes</option></select><select id="minq"><option value="0">Any qualification</option><option value="60">Qualification ≥60</option><option value="70">Qualification ≥70</option><option value="80">Qualification ≥80</option></select></div></header><main><div class="summary" id="sum"></div><div id="list"></div></main>
<script>const data=__DATA__;const q=document.getElementById('q'),rec=document.getElementById('rec'),chg=document.getElementById('chg'),active=document.getElementById('active'),lane=document.getElementById('lane'),minq=document.getElementById('minq'),list=document.getElementById('list'),sum=document.getElementById('sum');[...new Set(data.map(x=>x.career_lane).filter(Boolean))].sort().forEach(x=>lane.add(new Option(x,x)));function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}function safeHref(s){try{const u=new URL(String(s||''));return ['http:','https:'].includes(u.protocol)?u.href:'#'}catch(e){return '#'}}function render(){let v=data.filter(x=>(!rec.value||x.recommendation===rec.value)&&(!chg.value||x.change_status===chg.value)&&(!active.value||String(Number(x.is_active||0))===active.value)&&(!lane.value||x.career_lane===lane.value)&&(Number(x.qualification_score||0)>=Number(minq.value||0))&&(!q.value||(x.title+' '+x.company+' '+x.description).toLowerCase().includes(q.value.toLowerCase())));const counts={};v.forEach(x=>counts[x.recommendation]=(counts[x.recommendation]||0)+1);sum.innerHTML=`<div class="chip"><b>${v.length}</b> matching</div>`+Object.entries(counts).map(([k,n])=>`<div class="chip"><b>${n}</b> ${esc(k)}</div>`).join('');list.innerHTML=v.slice(0,1500).map(x=>`<section class="job ${esc(x.recommendation)} ${Number(x.is_active||0)?'':'inactive'}"><div class="top"><div><b>${esc(x.title)}</b><div>${esc(x.company)}</div></div><div class="scores">Door ${x.door_score} · Land ${x.landing_score} · Qual ${x.qualification_score} · Career ${x.career_score}</div></div><div class="meta">${esc(x.location_raw)} · ${esc(x.salary_text||'salary unknown')} · ${esc(x.posted_at||'date unknown')}</div><span class="tag ${esc(x.change_status)}">${esc(x.change_status||'')}</span><span class="tag">${Number(x.is_active||0)?'ACTIVE':'CLOSED'}</span><span class="tag">${esc(x.recommendation)}</span><span class="tag">Rel ${esc(x.relevance_score)}</span><span class="tag">${esc(x.normalized_title_family)}</span><span class="tag">${esc(x.resume_variant)}</span><span class="tag">${esc(x.source_count)} source(s)</span><span class="tag">canonical: ${esc(x.canonical_source_site)}</span><span class="tag">${esc(x.employment_class)}</span><span class="tag">source: ${esc(x.source_verification)}</span><div><a target="_blank" rel="noopener noreferrer" referrerpolicy="no-referrer" href="${esc(safeHref(x.apply_url||x.canonical_url))}">Open / apply</a> · <code>${esc(x.job_id)}</code></div><details><summary>Evidence, gaps & description</summary><p class="ok"><b>Evidence:</b> ${esc((x.requirement_matches||[]).join(' · ')||'none extracted')}</p><p class="gap"><b>Gaps:</b> ${esc((x.requirement_gaps||[]).join(' · ')||'none detected')}</p><p>${esc((x.score_reasons||[]).join(' · '))}</p>${x.hard_reject_reasons?.length?`<p class="gap"><b>Hard gate:</b> ${esc(x.hard_reject_reasons.join('; '))}</p>`:''}<p><b>Ledger:</b> first ${esc(x.first_seen)} · last ${esc(x.last_seen)} · changed ${esc(x.last_changed_at||'never')} · updates ${esc(x.update_count||0)}</p><p>${esc(x.description||'')}</p></details></section>`).join('')+(v.length>1500?`<p>Showing first 1,500 of ${v.length} matching jobs. Use CSV/SQLite for the complete ledger.</p>`:'')}q.oninput=rec.onchange=chg.onchange=active.onchange=lane.onchange=minq.onchange=render;render();</script>"""
    (out/"jobs.html").write_text(template.replace("__DATA__",js),encoding="utf-8")


def export_updates(store:PrecisionStore,out:Path)->None:
    rows=store.conn.execute("""SELECT j.job_id,j.change_status,j.last_changed_at,j.update_count,j.title,j.company,j.recommendation,j.is_active,j.apply_url,j.canonical_url,
      v.version_no,v.reason,v.observed_at,v.diff_json FROM jobs j LEFT JOIN job_versions v ON v.version_id=(SELECT vv.version_id FROM job_versions vv WHERE vv.job_id=j.job_id ORDER BY vv.version_no DESC LIMIT 1)
      WHERE j.change_status IN ('NEW','UPDATED') AND (j.change_ack_at IS NULL OR j.change_ack_at<j.last_changed_at) ORDER BY j.last_changed_at DESC""").fetchall()
    fields=["job_id","change_status","last_changed_at","update_count","title","company","recommendation","is_active","version_no","reason","apply_url","canonical_url","diff_json"]
    with (out/"updates.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for r in rows: w.writerow({k:csv_safe_cell(r[k]) for k in fields})
    lines=["# Unacknowledged Job Changes",f"Generated: {now_iso()}",f"Items: {len(rows)}",""]
    for r in rows:
        lines += [f"## {r['change_status']} — {r['title']} — {r['company']}",f"Job: `{r['job_id']}` · Version {r['version_no']} · {r['reason']} · {r['observed_at']}"]
        try:d=json.loads(r["diff_json"] or "{}")
        except Exception:d={}
        for k,v in d.items():
            if k=="description" and isinstance(v,dict):
                lines.append(f"- Description changed ({v.get('old_chars',0)} → {v.get('new_chars',0)} chars)")
                for x in v.get("added",[])[:5]: lines.append(f"  + {x[:280]}")
                for x in v.get("removed",[])[:5]: lines.append(f"  - {x[:280]}")
            elif isinstance(v,dict): lines.append(f"- {k}: `{v.get('old')}` → `{v.get('new')}`")
        lines += [f"- URL: {r['apply_url'] or r['canonical_url']}",""]
    (out/"updates.md").write_text("\n".join(lines),encoding="utf-8")
    build_updates_html(store,out)


def export_all(store:PrecisionStore,out:Path,strategy:dict[str,Any],config:dict[str,Any],mode:str):
    out.mkdir(parents=True,exist_ok=True); rows=store.rows()
    fields=["job_id","change_status","last_changed_at","update_count","is_active","recommendation","application_priority_score","door_score","landing_score","career_score","relevance_score","qualification_score","urgency_score","application_friction_score","eligibility_confidence","remote_confidence","source_confidence","extraction_confidence","title","company","location_raw","salary_text","posted_at","posting_status","application_deadline","normalized_title_family","career_lane","search_profile","resume_variant","remote_gate","employment_type","employment_class","employment_reason","source_verification","source_verification_reason","canonical_verified","recall_reason","travel_percent","timezone_requirement","work_auth_gate","work_authorization_requirement","salary_annual_mid","canonical_source_site","canonical_url","apply_url","application_status","notes","first_seen","last_seen"]
    done={"applied","screen","interview","final_interview","offer","rejected","withdrawn"}
    for name,pred in [
        ("all_discovered",lambda r:True),
        ("active_jobs",lambda r:int(r["is_active"] or 0)==1),
        ("candidate_universe",lambda r:int(r["is_active"] or 0)==1 and float(r["relevance_score"] or 0)>=65),
        ("qualified_universe",lambda r:int(r["is_active"] or 0)==1 and r["application_status"] not in done and r["recommendation"] in {"APPLY_NOW","APPLY_VOLUME","HIGH_VALUE_STRETCH","REVIEW","REVIEW_REMOTE","VERIFY_SOURCE","REVIEW_EMPLOYMENT"}),
        ("application_reservoir",lambda r:int(r["is_active"] or 0)==1 and r["application_status"] not in done and r["recommendation"] in {"APPLY_NOW","APPLY_VOLUME","HIGH_VALUE_STRETCH"}),
        ("verification_queue",lambda r:int(r["is_active"] or 0)==1 and r["recommendation"]=="VERIFY_SOURCE"),
        ("employment_review",lambda r:int(r["is_active"] or 0)==1 and r["recommendation"]=="REVIEW_EMPLOYMENT"),
        ("contract_review",lambda r:int(r["is_active"] or 0)==1 and r["recommendation"]=="CONTRACT_REVIEW"),
        ("part_time_review",lambda r:int(r["is_active"] or 0)==1 and r["recommendation"]=="PART_TIME_REVIEW"),
        ("apply_now",lambda r:int(r["is_active"] or 0)==1 and r["recommendation"]=="APPLY_NOW"),
        ("apply_volume",lambda r:int(r["is_active"] or 0)==1 and r["recommendation"]=="APPLY_VOLUME"),
        ("stretch",lambda r:int(r["is_active"] or 0)==1 and r["recommendation"]=="HIGH_VALUE_STRETCH"),
        ("review",lambda r:int(r["is_active"] or 0)==1 and r["recommendation"] in {"REVIEW","REVIEW_REMOTE"}),
        ("closed",lambda r:int(r["is_active"] or 0)==0),
    ]:
        with (out/f"{name}.csv").open("w",newline="",encoding="utf-8-sig") as f:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
            for r in rows:
                if pred(r): w.writerow({k:csv_safe_cell(r[k]) for k in fields})
    with (out/"jobs.jsonl").open("w",encoding="utf-8") as f:
        for r in rows: f.write(json.dumps(dict(r),ensure_ascii=False)+"\n")
    build_dashboard(store,out); picked=build_daily_plan(store,out,strategy); export_updates(store,out); build_funnel_report(store,out,strategy)
    candidates=[r for r in rows if int(r["is_active"] or 0)==1 and r["recommendation"] in {"APPLY_NOW","APPLY_VOLUME","HIGH_VALUE_STRETCH","REVIEW","VERIFY_SOURCE","REVIEW_EMPLOYMENT"} and r["application_status"] not in done]
    bd=out/"chatgpt_batches"; bd.mkdir(exist_ok=True)
    for old in bd.glob("*.md"): old.unlink()
    for bi in range(0,len(candidates),10):
        parts=["# Precision Job Decision Batch\n\nUse the extracted evidence/gaps. Do not assume unlisted experience.\n"]
        for r in candidates[bi:bi+10]:
            parts.append(f"\n## {r['job_id']} — {r['title']} — {r['company']}\nRecommendation: {r['recommendation']} | Door {r['door_score']} | Landing-fit {r['landing_score']} | Qualification {r['qualification_score']} | Career {r['career_score']} | Relevance {r['relevance_score']}\nFamily: {r['normalized_title_family']} | Lane: {r['career_lane']} | Remote: {r['remote_gate']} | Salary: {r['salary_text'] or 'unknown'} | Posted: {r['posted_at'] or 'unknown'}\nURL: {r['apply_url'] or r['canonical_url']}\nEvidence: {'; '.join(jsoncol(r,'requirement_matches_json'))}\nGaps: {'; '.join(jsoncol(r,'requirement_gaps_json'))}\nReasons: {'; '.join(jsoncol(r,'score_reasons_json'))}\n\nDescription:\n{(r['description'] or '')[:9000]}\n")
        (bd/f"batch_{bi//10+1:03d}.md").write_text("\n".join(parts),encoding="utf-8")
    recs={}; lanes={}; sources={}
    for r in rows:
        recs[r["recommendation"]]=recs.get(r["recommendation"],0)+1; lanes[r["career_lane"] or "unclassified"]=lanes.get(r["career_lane"] or "unclassified",0)+1
    for o in store.conn.execute("SELECT source_site,COUNT(*) n FROM occurrences GROUP BY source_site"): sources[o["source_site"]]=o["n"]
    reservoir=sum(1 for r in rows if int(r["is_active"] or 0)==1 and r["application_status"] not in done and r["recommendation"] in {"APPLY_NOW","APPLY_VOLUME","HIGH_VALUE_STRETCH"})
    changes=store.conn.execute("SELECT COUNT(*) n FROM jobs WHERE change_status IN ('NEW','UPDATED') AND (change_ack_at IS NULL OR change_ack_at<last_changed_at)").fetchone()["n"]
    report=["# Search Run Market Report",f"Generated: {now_iso()}",f"Total canonical jobs retained: **{len(rows)}**",f"Active qualified application reservoir: **{reservoir}**",f"Unacknowledged new/updated jobs: **{changes}**",f"Daily application slate: **{len(picked)}**",progress_summary(store,strategy),"\n## Recommendation counts"]+[f"- {k}: {v}" for k,v in sorted(recs.items(),key=lambda x:-x[1])]+["\n## Career lanes"]+[f"- {k}: {v}" for k,v in sorted(lanes.items(),key=lambda x:-x[1])]+["\n## Source occurrences"]+[f"- {k}: {v}" for k,v in sorted(sources.items(),key=lambda x:-x[1])]
    (out/"market_report.md").write_text("\n".join(report)+"\n",encoding="utf-8")


def print_summary(store:PrecisionStore):
    rows=store.rows(); rec={}
    for r in rows: rec[r["recommendation"]]=rec.get(r["recommendation"],0)+1
    print(f"Unique jobs retained: {len(rows)}")
    for k,v in sorted(rec.items(),key=lambda x:-x[1]): print(f"  {k:22s} {v}")
    print("\nTop application candidates:"); shown=0
    for r in rows:
        if r["recommendation"] not in {"APPLY_NOW","APPLY_VOLUME","HIGH_VALUE_STRETCH"}: continue
        print(f"  {r['door_score']:5.1f} {r['recommendation']:<18} Q={r['qualification_score']:>5.1f} R={r['relevance_score']:>5.1f} {r['title']} — {r['company']}"); shown+=1
        if shown>=15: break






def enrich_public_jsonld(client:HttpClient,job:Job)->Job:
    """Best-effort public JobPosting JSON-LD enrichment for ATSs not covered by core adapters."""
    url=job.apply_url or job.canonical_url
    if not url or is_restricted_url(url): return job
    try:
        raw=client.get_bytes(url,"text/html,application/xhtml+xml",allow_stale=True).decode("utf-8",errors="replace")
        scripts=re.findall(r'<script[^>]+type=["\\\']application/ld\\+json["\\\'][^>]*>(.*?)</script>',raw,flags=re.I|re.S)
        objs=[]
        for block in scripts:
            try:
                x=json.loads(block.strip())
                if isinstance(x,list): objs.extend(x)
                else: objs.append(x)
            except Exception: continue
        def walk(x):
            if isinstance(x,dict):
                if x.get("@type")=="JobPosting" or (isinstance(x.get("@type"),list) and "JobPosting" in x.get("@type")): return x
                for v in x.values():
                    z=walk(v)
                    if z: return z
            elif isinstance(x,list):
                for v in x:
                    z=walk(v)
                    if z: return z
            return None
        jp=None
        for x in objs:
            jp=walk(x)
            if jp: break
        if not jp: return job
        org=jp.get("hiringOrganization") or {}; loc=jp.get("jobLocation") or jp.get("applicantLocationRequirements") or ""
        if isinstance(loc,dict):
            addr=loc.get("address") or loc; loc=clean_text([addr.get("addressLocality"),addr.get("addressRegion"),addr.get("addressCountry")]) if isinstance(addr,dict) else clean_text(loc)
        elif isinstance(loc,list): loc=clean_text(loc)
        job.title=clean_text(jp.get("title") or job.title); job.company=clean_text(org.get("name") if isinstance(org,dict) else org) or job.company
        job.location_raw=clean_text(loc) or job.location_raw; job.description=strip_html(jp.get("description")) or job.description; job.posted_at=clean_text(jp.get("datePosted") or job.posted_at)
        job.employment_type=clean_text(jp.get("employmentType") or job.employment_type)
        job.raw["jsonld_enrichment"]=jp
    except Exception as e: job.raw["jsonld_enrichment_error"]=str(e)
    return job



def ats_identity(url:str)->tuple[str,str]:
    try:
        p=urllib.parse.urlsplit(url); host=(p.hostname or "").lower(); parts=[urllib.parse.unquote(x) for x in p.path.split("/") if x]
        if ("greenhouse.io" in host or "greenhouse.com" in host) and parts:
            # job-boards.greenhouse.io/<board>/jobs/<id>
            return "greenhouse",parts[0]
        if "jobs.ashbyhq.com" in host and parts: return "ashby",parts[0]
        if "jobs.lever.co" in host and parts: return "lever",parts[0]
        if ("jobs.smartrecruiters.com" in host or "careers.smartrecruiters.com" in host) and parts: return "smartrecruiters",parts[0]
    except Exception: pass
    return "",""


def load_ats_watch(path:Path,max_boards:int)->dict[str,list[dict[str,Any]]]:
    empty={"greenhouse":[],"lever":[],"ashby":[],"smartrecruiters":[]}
    if not path.exists(): return empty
    try: obj=json.loads(path.read_text(encoding="utf-8"))
    except Exception: return empty
    out={"greenhouse":[],"lever":[],"ashby":[],"smartrecruiters":[]}; n=0
    for typ in out:
        for x in obj.get(typ,[]):
            if n>=max_boards: break
            token=clean_text(x.get("token")); company=clean_text(x.get("company") or token)
            if not token: continue
            if typ=="greenhouse": out[typ].append({"enabled":True,"board":token,"company":company})
            elif typ=="lever": out[typ].append({"enabled":True,"site":token,"company":company})
            elif typ=="smartrecruiters": out[typ].append({"enabled":True,"company_identifier":token,"company":company})
            else: out[typ].append({"enabled":True,"board":token,"company":company})
            n+=1
    return out


def update_ats_watch(path:Path,jobs:list[Job],minimum_relevance:float=75,max_boards:int=100)->int:
    existing={"greenhouse":{},"lever":{},"ashby":{},"smartrecruiters":{}}
    if path.exists():
        try:
            old=json.loads(path.read_text(encoding="utf-8"))
            for typ in existing:
                for x in old.get(typ,[]): existing[typ][clean_text(x.get("token"))]=x
        except Exception: pass
    before=sum(len(x) for x in existing.values())
    for j in jobs:
        if float(getattr(j,"relevance_score",0))<minimum_relevance: continue
        typ,token=ats_identity(j.apply_url or j.canonical_url)
        if typ and token and token not in existing[typ]: existing[typ][token]={"token":token,"company":j.company,"discovered_at":now_iso()}
    obj={typ:list(vals.values())[:max_boards] for typ,vals in existing.items()}; path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(obj,indent=2,ensure_ascii=False),encoding="utf-8")
    return sum(len(x) for x in existing.values())-before

def fetch_jobicy_targeted(client:HttpClient,cfg:dict[str,Any])->list[Job]:
    urls=[]; base=cfg.get("url","")
    if cfg.get("include_unfiltered_latest",True): urls.append(base)
    parts=urllib.parse.urlsplit(base); q=dict(urllib.parse.parse_qsl(parts.query,keep_blank_values=True)); q["count"]="200"; q.setdefault("geo","usa")
    for tag in cfg.get("tags",[]):
        qq=dict(q); qq["tag"]=tag
        urls.append(urllib.parse.urlunsplit((parts.scheme,parts.netloc,parts.path,urllib.parse.urlencode(qq),"")))
    out=[]; seen=set()
    for u in urls:
        try:
            batch=c.fetch_jobicy(client,{"url":u})
            tag=dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query)).get("tag","latest")
            if len(batch)>=200:
                print(f"  ! Jobicy query '{tag}' hit the public API 200-result ceiling; targeted overlapping queries/direct ATS discovery are used to reduce missed tail risk",file=sys.stderr)
            for j in batch:
                key=j.source_job_id or j.canonical_url or (norm(j.company)+"|"+norm(j.title))
                if key in seen: continue
                seen.add(key); out.append(j)
        except Exception as e:
            print(f"  ! Jobicy query failed for tag={dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query)).get('tag','latest')}: {e}",file=sys.stderr)
    return out


def fetch_remotelanders_exhaustive(client:HttpClient,cfg:dict[str,Any])->list[Job]:
    """Continue until the API is exhausted; `safety_max_pages` is only a runaway guard."""
    base=clean_text(cfg.get("url")); size=max(1,min(100,int(cfg.get("page_size",100)))); max_pages=max(1,int(cfg.get("safety_max_pages",250)))
    out=[]; seen=set()
    for page in range(1,max_pages+1):
        sep="&" if "?" in base else "?"; obj=client.json(f"{base}{sep}limit={size}&page={page}")
        rows=obj.get("jobs",[]) if isinstance(obj,dict) else []
        if not rows: break
        for x in rows:
            key=clean_text(x.get("slug") or x.get("id") or x.get("url"))
            if key and key in seen: continue
            if key: seen.add(key)
            mn,mx,cur,per=c.parse_salary(x.get("salary"))
            j=Job(source_site="remotelanders",source_job_id=clean_text(x.get("slug") or x.get("id")),canonical_url=canonical_url(clean_text(x.get("url"))),apply_url=canonical_url(clean_text(x.get("applyUrl") or x.get("url"))),title=clean_text(x.get("title")),company=clean_text(x.get("company")),location_raw=clean_text(x.get("location") or "Remote"),remote_status="remote",employment_type=clean_text(x.get("type")),salary_text=clean_text(x.get("salary")),salary_min=mn,salary_max=mx,salary_currency=cur,salary_period=per,posted_at=clean_text(x.get("postedDate")),description=strip_html(x.get("description") or ""),category=clean_text(x.get("category")),tags=[clean_text(t) for t in x.get("subtags",[])],raw=x)
            out.append(j)
        if len(rows)<size: break
    else:
        print(f"  ! RemoteLanders hit safety_max_pages={max_pages}; raise it if the API still had results",file=sys.stderr)
    return out


def fetch_direct_ats_watch_v2(client:HttpClient,app_cfg:dict[str,Any])->tuple[list[Job],dict[tuple[str,str],set[str]]]:
    """Fetch entire configured public boards and return complete-scan membership for closure reconciliation."""
    out=[]; scans:dict[tuple[str,str],set[str]]={}; watch=app_cfg.get("ats_watch",{})
    for x in watch.get("greenhouse",[]) if isinstance(watch.get("greenhouse",[]),list) else []:
        if not x.get("enabled",True): continue
        board=clean_text(x.get("board")); company=clean_text(x.get("company") or board)
        if not board: continue
        try:
            obj=client.json(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs?content=true")
        except Exception as e:
            if "20 MiB" not in str(e): raise
            # Preserve exhaustive board membership even when descriptions make the aggregate
            # payload too large. Relevant lightweight rows are enriched individually later.
            obj=client.json(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board)}/jobs")
        key=("greenhouse",board); scans[key]=set()
        for r in obj.get("jobs",[]):
            sid=str(r.get("id", "")); scans[key].add(sid); loc=clean_text((r.get("location") or {}).get("name")); raw=dict(r); raw["_board"]=board
            out.append(Job(source_site="greenhouse",source_job_id=sid,canonical_url=canonical_url(r.get("absolute_url","")),apply_url=canonical_url(r.get("absolute_url","")),title=clean_text(r.get("title")),company=company,location_raw=loc,remote_status="remote" if "remote" in norm(loc+" "+clean_text(r.get("title"))) else "unknown",posted_at=clean_text(r.get("updated_at")),description=strip_html(r.get("content")),raw=raw))
    for x in watch.get("lever",[]) if isinstance(watch.get("lever",[]),list) else []:
        if not x.get("enabled",True): continue
        board=clean_text(x.get("site")); company=clean_text(x.get("company") or board)
        if not board: continue
        # Lever's public Postings API supports skip/limit pagination. Paginating prevents a
        # very large employer board from exceeding the global 20 MiB response safety ceiling.
        key=("lever",board); scans[key]=set(); skip=0; limit=100
        while True:
            obj=client.json(f"https://api.lever.co/v0/postings/{urllib.parse.quote(board)}?mode=json&skip={skip}&limit={limit}")
            rows=obj if isinstance(obj,list) else []
            for r in rows:
                sid=clean_text(r.get("id")); scans[key].add(sid); cat=r.get("categories") or {}; loc=clean_text(cat.get("location")); sal=r.get("salaryRange") or {}; raw=dict(r); raw["_board"]=board
                out.append(Job(source_site="lever",source_job_id=sid,canonical_url=canonical_url(r.get("hostedUrl","")),apply_url=canonical_url(r.get("applyUrl","")),title=clean_text(r.get("text")),company=company,location_raw=loc,remote_status="remote" if clean_text(r.get("workplaceType")).lower()=="remote" or "remote" in norm(loc) else clean_text(r.get("workplaceType")) or "unknown",employment_type=clean_text(cat.get("commitment")),salary_text=clean_text(r.get("salaryDescription")),salary_min=sal.get("min"),salary_max=sal.get("max"),salary_currency=clean_text(sal.get("currency") or "USD"),salary_period=clean_text(sal.get("interval") or "year"),posted_at=(datetime.fromtimestamp(float(r.get("createdAt"))/1000,tz=timezone.utc).isoformat() if isinstance(r.get("createdAt"),(int,float)) and float(r.get("createdAt"))>1e11 else clean_text(r.get("createdAt") or r.get("updatedAt"))),description=strip_html(r.get("descriptionPlain") or r.get("description")),category=clean_text(cat.get("team") or cat.get("department")),raw=raw))
            if len(rows)<limit: break
            skip += len(rows)
    for x in watch.get("ashby",[]) if isinstance(watch.get("ashby",[]),list) else []:
        if not x.get("enabled",True): continue
        board=clean_text(x.get("board")); company=clean_text(x.get("company") or board)
        if not board: continue
        obj=client.json(f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?includeCompensation=true"); key=("ashby",board); scans[key]=set()
        for r in obj.get("jobs",[]):
            sid=clean_text(r.get("id") or r.get("jobUrl")); scans[key].add(sid); comp=r.get("compensation") or {}; st=clean_text(comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary")); mn,mx,cur,per=c.parse_salary(st); raw=dict(r); raw["_board"]=board
            out.append(Job(source_site="ashby",source_job_id=sid,canonical_url=canonical_url(r.get("jobUrl","")),apply_url=canonical_url(r.get("applyUrl","")),title=clean_text(r.get("title")),company=company,location_raw=clean_text(r.get("location")),remote_status="remote" if "remote" in norm(r.get("workplaceType") or r.get("location")) else clean_text(r.get("workplaceType")) or "unknown",employment_type=clean_text(r.get("employmentType")),salary_text=st,salary_min=mn,salary_max=mx,salary_currency=cur,salary_period=per,posted_at=clean_text(r.get("publishedAt")),description=strip_html(r.get("descriptionPlain") or r.get("descriptionHtml")),category=clean_text(r.get("department") or r.get("team")),raw=raw))
    for x in watch.get("smartrecruiters",[]) if isinstance(watch.get("smartrecruiters",[]),list) else []:
        if not x.get("enabled",True): continue
        board=clean_text(x.get("company_identifier") or x.get("board")); company=clean_text(x.get("company") or board)
        if not board: continue
        key=("smartrecruiters",board); scans[key]=set(); offset=0; limit=100; total=None
        while total is None or offset<total:
            obj=client.json(f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(board)}/postings?destination=PUBLIC&limit={limit}&offset={offset}")
            rows=obj.get("content",[]) if isinstance(obj,dict) else []; total=int(obj.get("totalFound",len(rows)) or len(rows)) if isinstance(obj,dict) else len(rows)
            if not rows: break
            for summary in rows:
                sid=clean_text(summary.get("id") or summary.get("uuid"));
                if not sid: continue
                scans[key].add(sid)
                try: r=client.json(f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(board)}/postings/{urllib.parse.quote(sid)}")
                except Exception: r=summary
                loc=r.get("location") or {}; loc_text=clean_text([loc.get("city"),loc.get("region") or loc.get("regionCode"),loc.get("country") or loc.get("countryCode")]) if isinstance(loc,dict) else clean_text(loc)
                secs=((r.get("jobAd") or {}).get("sections") or {}) if isinstance(r.get("jobAd") or {},dict) else {}
                desc_parts=[]
                for sk in ("companyDescription","jobDescription","qualifications","additionalInformation"):
                    sec=secs.get(sk) or {}
                    if isinstance(sec,dict):
                        tt=clean_text(sec.get("title")); tx=strip_html(sec.get("text"));
                        if tx: desc_parts.append((tt+"\n" if tt else "")+tx)
                comp=r.get("compensation") or {}; st=""
                if isinstance(comp,dict) and (comp.get("min") is not None or comp.get("max") is not None):
                    st=f"{comp.get('min','')} - {comp.get('max','')} {clean_text(comp.get('currency') or '')} {clean_text(comp.get('period') or '')}".strip()
                mn,mx,cur,per=c.parse_salary(st,comp.get("min") if isinstance(comp,dict) else None,comp.get("max") if isinstance(comp,dict) else None,clean_text(comp.get("currency") if isinstance(comp,dict) else "") or "USD",clean_text(comp.get("period") if isinstance(comp,dict) else ""))
                et=r.get("typeOfEmployment") or {}; dept=r.get("department") or {}; fun=r.get("function") or {}; raw=dict(r); raw["_board"]=board
                out.append(Job(source_site="smartrecruiters",source_job_id=sid,canonical_url=canonical_url(r.get("postingUrl") or summary.get("postingUrl") or summary.get("ref") or ""),apply_url=canonical_url(r.get("applyUrl") or summary.get("applyUrl") or ""),title=clean_text(r.get("name") or summary.get("name")),company=clean_text((r.get("company") or {}).get("name") if isinstance(r.get("company"),dict) else "") or company,location_raw=loc_text,remote_status="remote" if isinstance(loc,dict) and bool(loc.get("remote")) else "unknown",employment_type=clean_text(et.get("label") if isinstance(et,dict) else et),salary_text=st,salary_min=mn,salary_max=mx,salary_currency=cur,salary_period=per,posted_at=clean_text(r.get("releasedDate") or summary.get("releasedDate")),description="\n\n".join(desc_parts),category=clean_text((dept.get("label") if isinstance(dept,dict) else dept) or (fun.get("label") if isinstance(fun,dict) else fun)),raw=raw))
            offset += len(rows)
            if len(rows)<limit: break
    return out,scans


def _watch_token(typ:str,x:dict[str,Any])->str:
    if typ=="lever": return clean_text(x.get("site") or x.get("token"))
    if typ=="smartrecruiters": return clean_text(x.get("company_identifier") or x.get("board") or x.get("token"))
    return clean_text(x.get("board") or x.get("token"))


def merge_ats_watch(*watches:dict[str,Any])->dict[str,list[dict[str,Any]]]:
    """Merge configured + learned public ATS boards without duplicate network scans."""
    out={"greenhouse":[],"lever":[],"ashby":[],"smartrecruiters":[]}
    seen={k:set() for k in out}
    for watch in watches:
        watch=(watch or {}).get("ats_watch",watch or {})
        for typ in out:
            rows=watch.get(typ,[]) if isinstance(watch.get(typ,[]),list) else []
            for x in rows:
                token=_watch_token(typ,x)
                if not token or token.lower() in seen[typ]: continue
                seen[typ].add(token.lower()); out[typ].append(dict(x))
    return out


def fetch_direct_ats_watch_resilient(client:HttpClient,watch:dict[str,Any],runtime:Optional[dict[str,Any]]=None)->tuple[list[Job],dict[tuple[str,str],set[str]],list[str]]:
    """Scan public employer boards with bounded concurrency and visible progress.

    Each board is isolated, uses a shorter ATS-specific timeout, and caches successful responses
    briefly so an interrupted run can be restarted without repeating every completed network call.
    """
    runtime=runtime or {}
    watch=(watch or {}).get("ats_watch",watch or {})
    entries=[]
    for typ in ("greenhouse","lever","ashby","smartrecruiters"):
        rows=watch.get(typ,[]) if isinstance(watch.get(typ,[]),list) else []
        for x in rows:
            if x.get("enabled",True) and _watch_token(typ,x): entries.append((typ,dict(x)))
    total=len(entries)
    if not total: return [],{},[]
    max_workers=max(1,min(12,int(runtime.get("max_workers",6) or 6)))
    timeout=max(4,min(30,int(runtime.get("board_timeout_seconds",10) or 10)))
    cache_minutes=max(0,int(runtime.get("restart_cache_minutes",60) or 60))
    print(f"[ats-watch] scanning {total} employer ATS board(s) with {max_workers} worker(s); per-board HTTP timeout={timeout}s")

    def scan_one(item:tuple[str,dict[str,Any]]):
        typ,x=item; token=_watch_token(typ,x) or "?"
        # A board-specific client prevents a single slow board from inheriting the 25s general timeout.
        bclient=HttpClient(client.cache_dir,max(client.cache_minutes,cache_minutes),timeout,client.user_agent,attempts=1)
        try:
            got,ss=fetch_direct_ats_watch_v2(bclient,{"ats_watch":{typ:[x]}})
            return typ,token,got,ss,None
        except Exception as e:
            return typ,token,[],{},f"{typ}:{token}: {e}"

    out=[]; scans={}; errors=[]; done=0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers,thread_name_prefix="ats") as pool:
        futs=[pool.submit(scan_one,item) for item in entries]
        for fut in concurrent.futures.as_completed(futs):
            typ,token,got,ss,err=fut.result(); done+=1
            if err:
                errors.append(err)
                print(f"[ats-watch] {done}/{total} FAIL {typ}:{token} — {err.split(': ',1)[-1]}",file=sys.stderr)
            else:
                out.extend(got); scans.update(ss)
                print(f"[ats-watch] {done}/{total} OK   {typ}:{token} — {len(got)} posting(s)")
    return out,scans,errors


def _bundle_queries(strategy:dict[str,Any],mode:str,bundle_size:int=4)->list[tuple[str,str]]:
    allowed=set(strategy.get("strategy",{}).get("run_modes",{}).get(mode,{}).get("profiles",[])); pairs=[]
    for p in sorted([x for x in strategy.get("searches",[]) if x.get("enabled",True) and (not allowed or x.get("name") in allowed)],key=lambda x:int(x.get("priority",3))):
        kws=[clean_text(x) for x in p.get("keywords",[]) if clean_text(x)]
        for i in range(0,len(kws),bundle_size):
            chunk=kws[i:i+bundle_size]
            q="("+" OR ".join('"'+x.replace('"','')+'"' for x in chunk)+")"
            pairs.append((clean_text(p.get("name")),q))
    return pairs


def platform_search_url(platform:str,query:str,window_days:int)->str:
    q=urllib.parse.quote_plus(query)
    if platform=="linkedin":
        sec=86400 if window_days<=1 else 604800 if window_days<=7 else 2592000
        return f"https://www.linkedin.com/jobs/search/?keywords={q}&location=United%20States&f_WT=2&f_TPR=r{sec}&sortBy=DD"
    if platform=="indeed":
        return f"https://www.indeed.com/jobs?q={q}&l=Remote&fromage={max(1,window_days)}&sort=date"
    if platform=="glassdoor":
        # Native Glassdoor search. The user should visually confirm its Remote filter remains selected.
        return f"https://www.glassdoor.com/Job/jobs.htm?sc.keyword={q}&locKeyword=Remote"
    return c.search_url(platform,query)


def make_coverage_segments(strategy:dict[str,Any],config:dict[str,Any],mode:str)->list[dict[str,Any]]:
    ccfg=config.get("coverage",{}); platforms=[x for x in ccfg.get("primary_platforms",["linkedin","indeed","glassdoor"]) if config.get("manual_platforms",{}).get(x,True)]
    windows=ccfg.get("windows_days",[1,7,30] if mode=="deep" else [1,7]); bs=max(1,int(ccfg.get("boolean_bundle_size",4))); pairs=_bundle_queries(strategy,mode,bs); out=[]
    for plat in platforms:
        for profile,q in pairs:
            for days in windows:
                raw=f"{plat}|{mode}|{profile}|{days}|{q}"; sid="S"+hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
                note=(f"Glassdoor: after opening, manually set Date Posted to the closest available {int(days)}-day window before marking complete; the native URL does not reliably encode this filter." if plat=="glassdoor" else "")
                out.append({"segment_id":sid,"platform":plat,"mode":mode,"search_profile":profile,"query_text":q,"window_days":int(days),"search_url":platform_search_url(plat,q,int(days)),"notes":note})
    return out


def build_coverage_dashboard(store:PrecisionStore,out:Path)->None:
    rows=store.coverage_rows(); counts={}; due=0
    for r in rows:
        counts[r["platform"]]=counts.get(r["platform"],0)+1
        if r["status"]=="due": due+=1
    body=["<!doctype html><meta charset='utf-8'><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'none'\"><meta name='referrer' content='no-referrer'><title>Search Coverage</title><style>body{font-family:system-ui;margin:24px;background:#f6f8fb;color:#172033}table{width:100%;border-collapse:collapse;background:white}th,td{padding:8px;border-bottom:1px solid #e1e5eb;text-align:left;vertical-align:top}.due{background:#fff7ed}.done{opacity:.72}input{padding:9px;width:min(520px,90%)}a{color:#1359b2}</style>",f"<h1>LinkedIn / Indeed / Glassdoor Coverage Ledger</h1><p>{len(rows)} search segments · {due} due/unconfirmed. These are normal user-browsing searches; the collector does not silently crawl restricted boards.</p><input id='q' placeholder='Filter profile/platform/query'><table id='t'><thead><tr><th>Status</th><th>Platform</th><th>Window</th><th>Profile</th><th>Boolean query</th><th>Last completed</th><th>Open</th><th>ID</th></tr></thead><tbody>"]
    for r in rows:
        done=bool(r["last_completed_at"] and r["status"]=="complete"); superseded=r["status"]=="superseded"; cls="done" if done or superseded else "due"; u=c.safe_output_url(r["search_url"])
        body.append(f"<tr class='{cls}'><td>{'superseded' if superseded else 'complete' if done else 'DUE'}</td><td>{c.html.escape(r['platform'])}</td><td>{r['window_days']}d</td><td>{c.html.escape(r['search_profile'])}</td><td>{c.html.escape(r['query_text'])}</td><td>{c.html.escape(r['last_completed_at'] or 'never')}</td><td><a target='_blank' rel='noopener noreferrer' referrerpolicy='no-referrer' href='{c.html.escape(u,quote=True)}'>open</a></td><td><code>{r['segment_id']}</code><br><small>{c.html.escape(r['notes'] or '')}</small></td></tr>")
    body.append("</tbody></table><p>If a board reports a result cap (for example LinkedIn can return up to 1,000 results for a search), split it with <code>python jobbot.py coverage-split SEGMENT_ID</code>. After fully reviewing one search segment, run <code>python jobbot.py coverage-done SEGMENT_ID</code>. Capture useful postings with <code>python jobbot.py capture --platform PLATFORM</code>.</p><script>const q=document.getElementById('q');q.oninput=()=>{for(const r of document.querySelectorAll('#t tbody tr'))r.style.display=r.innerText.toLowerCase().includes(q.value.toLowerCase())?'':'none'}</script>")
    (out/"coverage.html").write_text("".join(body),encoding="utf-8")
    fields=["segment_id","platform","mode","search_profile","query_text","window_days","status","last_opened_at","last_completed_at","completed_count","search_url","notes"]
    with (out/"coverage.csv").open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); [w.writerow({k:csv_safe_cell(r[k]) for k in fields}) for r in rows]


def run_search(config:dict[str,Any],strategy:dict[str,Any],mode:str)->int:
    started=now_iso(); base=Path(config["_base"]); ac=config.get("app",{}); out=abs_path(base,ac.get("output_dir","out")); db=abs_path(base,ac.get("db_path","data/jobs.sqlite3")); cache=abs_path(base,ac.get("cache_dir","cache"))
    client=HttpClient(cache,int(ac.get("cache_minutes",45)),int(ac.get("http_timeout_seconds",25)),clean_text(ac.get("user_agent")) or "RemoteCareerJobSearch/2.0")
    store=PrecisionStore(db); run_id=store.begin_run(started,mode); source_status={}; jobs:list[Job]=[]; board_scans:dict[tuple[str,str],set[str]]={}
    safety_max=int(ac.get("safety_max_jobs_per_source",50000))
    # Upgrade/migration path: reclassify the persistent ledger with the current strategy BEFORE
    # discovery. This lets a v2.0 database recover previously missed empty-description ATS leads.
    stale=store.conn.execute("SELECT COUNT(*) n FROM jobs WHERE COALESCE(strategy_version,'')<>?",(VERSION,)).fetchone()["n"]
    if stale:
        lcfg=config.get("ledger",{})
        if lcfg.get("auto_backup_before_strategy_migration",True) and db.exists():
            bdir=abs_path(base,lcfg.get("backup_dir","data/backups")); bdir.mkdir(parents=True,exist_ok=True)
            stamp=utcnow().strftime("%Y%m%dT%H%M%SZ"); backup_path=bdir/f"jobs_before_{VERSION.replace('.','_')}_{stamp}.sqlite3"
            dest=sqlite3.connect(str(backup_path))
            try: store.conn.backup(dest)
            finally: dest.close()
            print(f"[migration] consistent SQLite backup created: {backup_path}")
        print(f"[migration] rescoring {stale} existing canonical jobs with v{VERSION} recall logic...")
        migrated=store.rescore_all(strategy,config.get("candidate",{}),"deep")
        print(f"[migration] reclassified ledger: {migrated}")
    # Seed the learned employer-board universe from the ENTIRE persistent candidate universe,
    # including direct ATS links that v2.0 could not enrich because their descriptions were blank.
    ad=config.get("ats_discovery",{}); ats_file=abs_path(base,ad.get("file","data/discovered_ats.json"))
    if ad.get("enabled",True):
        ledger_candidates=[]
        seed_min=float(ad.get("seed_minimum_relevance",65))
        for r in store.conn.execute("SELECT * FROM jobs WHERE is_active=1 AND COALESCE(relevance_score,0)>=?",(seed_min,)).fetchall():
            jj=job_from_row(r); jj.relevance_score=float(r["relevance_score"] or 0); ledger_candidates.append(jj)
        seeded=update_ats_watch(ats_file,ledger_candidates,seed_min,int(ad.get("max_boards",1000)))
        if seeded: print(f"[migration] learned {seeded} employer ATS board(s) from the existing ledger before retrieval")
    for name,fn in [("remotive",c.fetch_remotive),("jobicy",fetch_jobicy_targeted),("remoteok",c.fetch_remoteok),("remotelanders",fetch_remotelanders_exhaustive)]:
        sc=config.get("sources",{}).get(name,{})
        if not sc.get("enabled",False): continue
        try:
            print(f"[{name}] retrieving exhaustively within source/search boundaries...")
            source_client=client
            min_poll=int(sc.get("minimum_poll_minutes",0) or 0)
            if min_poll>0:
                source_client=HttpClient(cache,max(int(ac.get("cache_minutes",0)),min_poll),int(ac.get("http_timeout_seconds",25)),clean_text(ac.get("user_agent")) or "RemoteCareerJobSearch/2.0")
            got=fn(source_client,sc)
            if len(got)>safety_max: raise RuntimeError(f"source returned {len(got)} rows, exceeding safety ceiling {safety_max}; raise app.safety_max_jobs_per_source intentionally")
            jobs.extend(got); source_status[name]={"ok":True,"count":len(got),"complete":True}; print(f"[{name}] {len(got)} rows")
        except Exception as e:
            source_status[name]={"ok":False,"error":str(e),"complete":False}; print(f"[{name}] ERROR: {e}",file=sys.stderr)
    # Configured + learned employer boards are merged and scanned once each. A failing board is
    # isolated; it never discards successful results from the other public ATS boards.
    dyn=load_ats_watch(ats_file,int(ad.get("max_boards",1000))) if ad.get("enabled",True) else {"greenhouse":[],"lever":[],"ashby":[],"smartrecruiters":[]}
    merged_watch=merge_ats_watch(config.get("ats_watch",{}),dyn)
    ats,dscans,ats_errors=fetch_direct_ats_watch_resilient(client,merged_watch,config.get("ats_runtime",{}))
    jobs.extend(ats); board_scans.update(dscans)
    source_status["ats_watch"]={"ok":len(ats_errors)==0,"count":len(ats),"boards_complete":len(dscans),"board_errors":len(ats_errors),"errors":ats_errors[:30],"complete":len(ats_errors)==0}
    print(f"[ats-watch] {len(ats)} postings from {len(dscans)} complete board scan(s); {len(ats_errors)} board error(s)")
    for j in jobs:
        setattr(j,"_mode",mode)
        if isinstance(j.raw,dict):
            j.raw.setdefault("_discovery_company",j.company); j.raw.setdefault("_discovery_title",j.title)
    # v2.1 recall-first canonical enrichment. This occurs BEFORE final role classification.
    # A missing description must never prevent us from fetching the employer ATS description.
    enrich=config.get("enrichment",{}); max_enrich=int(enrich.get("safety_max_enrichments_per_run",5000)); enrich_count=0; recall_candidates=0
    if enrich.get("public_ats",True):
        for j in jobs:
            recall_ok,recall_reason=recall_prefilter(j,strategy)
            prelim,_,_=pick_profile(j,strategy,mode)
            if not (recall_ok or prelim) or not j.apply_url: continue
            recall_candidates+=1
            if isinstance(j.raw,dict): j.raw["_recall_reason"]=recall_reason
            h=host_of(j.apply_url); direct_supported=any(x in h for x in ("lever.co","greenhouse.io","ashbyhq.com"))
            is_other_public=any(x in h for x in ("smartrecruiters.com","recruitee.com","workdayjobs.com","myworkdayjobs.com","jobvite.com","icims.com"))
            should=direct_supported and (j.source_site not in {"lever","greenhouse","ashby"} or not j.description)
            should=should or ((not j.description or is_other_public) and bool(j.apply_url) and bool(enrich.get("jsonld_fallback",True)))
            if not should: continue
            if max_enrich and enrich_count>=max_enrich:
                print(f"  ! enrichment safety ceiling reached ({max_enrich}); remaining recall candidates stay in ledger for a later run",file=sys.stderr); break
            if direct_supported: c.enrich_public_ats(client,j)
            else: enrich_public_jsonld(client,j)
            enrich_count+=1
        print(f"[enrichment] recall candidates={recall_candidates}; canonical/detail enrichments attempted={enrich_count}")
    new=updated=unchanged=0; touched:set[str]=set()
    store.conn.execute("BEGIN")
    try:
        for j in jobs:
            score_job(j,strategy,config.get("candidate",{})); st=store.upsert(j,run_id=run_id,commit=False); touched.add(store.resolve_job_id(j))
            if st=="new": new+=1
            elif st=="updated": updated+=1
            else: unchanged+=1
        store.conn.commit()
    except Exception:
        store.conn.rollback(); raise
    # Reconcile true closures only from complete employer-board scans. Two misses avoids transient API failures.
    closed=0; miss_threshold=int(config.get("ledger",{}).get("ats_close_after_complete_misses",2))
    for (site,board),seen_ids in board_scans.items():
        closed += store.reconcile_complete_board(site,board,seen_ids,run_id,miss_threshold)
    if ad.get("enabled",True):
        added_boards=update_ats_watch(ats_file,jobs,float(ad.get("minimum_relevance",75)),int(ad.get("max_boards",1000)))
        if added_boards: print(f"[ats-discovery] learned {added_boards} new employer board(s) for future exhaustive scans")
    # Keep a persistent assisted-search coverage ledger for LinkedIn / Indeed / Glassdoor.
    segments=make_coverage_segments(strategy,config,mode); store.sync_coverage_segments(segments)
    store.record_run(run_id,source_status,len(jobs),len(touched),new,updated,unchanged,closed)
    export_all(store,out,strategy,config,mode); build_coverage_dashboard(store,out)
    print(f"\nRaw source sightings this run: {len(jobs)} | canonical jobs touched: {len(touched)}")
    print(f"NEW: {new} | UPDATED (meaningful source diff): {updated} | UNCHANGED sightings: {unchanged} | CLOSED from complete ATS reconciliation: {closed}")
    print_summary(store); print("\n"+progress_summary(store,strategy))
    print(f"\nDaily plan: {out/'daily_apply_plan.md'}\nDashboard: {out/'jobs.html'}\nQualified reservoir: {out/'qualified_universe.csv'}\nChange feed: {out/'updates.csv'}\nCoverage ledger: {out/'coverage.html'}")
    store.close(); return 0




def resume_path_for(row:sqlite3.Row,base:Path)->Path:
    m={
      "enrollment_operations":"Elizabeth_Kim-Ortiz_Resume_Enrollment_Operations(1).docx",
      "healthcare_qa":"Elizabeth_Kim-Ortiz_Resume_Healthcare_QA(1).docx",
      "healthcare_qa_or_enrollment_ops":"Elizabeth_Kim-Ortiz_Resume_Enrollment_Operations(1).docx",
      "higher_ed_records":"Elizabeth_Kim-Ortiz_Resume_HigherEd_Records(1).docx",
      "content_quality":"Elizabeth_Kim-Ortiz_Resume_Content_Quality(1).docx",
    }
    return base/"resumes"/m.get(row["resume_variant"],m["enrollment_operations"])


def prepare_packet(store:PrecisionStore,out:Path,base:Path,job_id:str)->Path:
    r=store.conn.execute("SELECT * FROM jobs WHERE job_id=?",(job_id,)).fetchone()
    if not r: raise ValueError(f"Unknown job id: {job_id}")
    d=out/"application_packets"; d.mkdir(parents=True,exist_ok=True); rp=resume_path_for(r,base)
    lines=[f"# Application Packet — {r['title']} — {r['company']}","",f"Job ID: `{job_id}`",f"Recommendation: **{r['recommendation']}**",f"Priority {r['application_priority_score']:.1f} | Door {r['door_score']:.1f} | Landing-fit {r['landing_score']:.1f} | Qualification {r['qualification_score']:.1f} | Career {r['career_score']:.1f} | Relevance {r['relevance_score']:.1f}",f"Remote: {r['remote_gate']} ({r['remote_gate_reason']})",f"Work authorization: {r['work_auth_gate']} — {r['work_authorization_requirement'] or 'no explicit restriction extracted'}",f"Travel: {r['travel_percent'] if r['travel_percent'] is not None else 'not explicitly quantified'}% | Schedule/timezone: {r['timezone_requirement'] or 'none extracted'}",f"Salary: {r['salary_text'] or 'unknown'}",f"Posted: {r['posted_at'] or 'unknown'}",f"Apply: {r['apply_url'] or r['canonical_url']}","",f"## Resume to use\n`{rp}`","","## Evidence supporting application"]
    lines += [f"- {x}" for x in jsoncol(r,"requirement_matches_json")] or ["- No explicit requirement matches extracted; review manually."]
    lines += ["","## Gaps / items to verify"]+[f"- {x}" for x in jsoncol(r,"requirement_gaps_json")] if jsoncol(r,"requirement_gaps_json") else ["","## Gaps / items to verify","- None detected by the deterministic parser; still verify the posting before submitting."]
    lines += ["","## Submission checklist","- [ ] Confirm role is still fully remote and Texas-eligible","- [ ] Confirm no required qualification was missed by extraction","- [ ] Use the recommended resume (tailor wording truthfully if useful)","- [ ] Answer screening questions from actual experience only","- [ ] Submit through the employer/direct application page when possible","- [ ] Save confirmation / application ID","- [ ] Mark the job as applied: `python jobbot.py mark %s applied`"%job_id,"","## Full job description","",r["description"] or "(description unavailable)"]
    path=d/f"{job_id}.md"; path.write_text("\n".join(lines),encoding="utf-8"); return path


def prepare_daily_packets(store:PrecisionStore,out:Path,base:Path,strategy:dict[str,Any],target:Optional[int]=None)->list[Path]:
    picked=build_daily_plan(store,out,strategy,target); paths=[prepare_packet(store,out,base,r["job_id"]) for r in picked]
    (out/"application_packets").mkdir(parents=True,exist_ok=True)
    idx=out/"application_packets"/"TODAY.md"; idx.write_text("# Today's Application Packets\n\n"+"\n".join(f"- {p.name}" for p in paths)+"\n",encoding="utf-8"); return paths

def regression_test_from_html(path:Path,config:dict[str,Any],strategy:dict[str,Any])->int:
    text=path.read_text(encoding="utf-8",errors="replace"); m=re.search(r"const data=(\[.*?\]);const q=",text,re.S)
    if not m: raise RuntimeError("Could not locate dashboard job data")
    rows=json.loads(m.group(1)); bad_apply=[]; counts={}
    obvious_bad=["engineer","developer","architect","accountant","controller","account executive","territory manager","finance manager","recruiter","marketing director","product manager","data scientist"]
    for x in rows:
        j=Job(source_site="regression",source_job_id=x.get("job_id",""),canonical_url=x.get("canonical_url",""),apply_url=x.get("apply_url",""),title=x.get("title",""),company=x.get("company",""),location_raw=x.get("location_raw",""),remote_status="remote",employment_type=x.get("employment_type",""),salary_text=x.get("salary_text",""),posted_at=x.get("posted_at",""),description=x.get("description","")); setattr(j,"_mode","deep"); score_job(j,strategy,config.get("candidate",{})); counts[j.recommendation]=counts.get(j.recommendation,0)+1
        if j.recommendation in {"APPLY_NOW","APPLY_VOLUME"} and any(phrase_present(t,j.title) for t in obvious_bad): bad_apply.append(j.title)
    print("Regression counts:",counts); print("Obvious off-target jobs in apply queues:",len(bad_apply))
    for t in bad_apply[:20]: print("  BAD:",t)
    if bad_apply: return 1
    print("REGRESSION TEST PASSED"); return 0


def self_test(config:dict[str,Any],strategy:dict[str,Any])->int:
    def gh(label:str,title:str,desc:str,location:str="Remote - United States",employment:str="Full-time",jid:int=1000)->tuple[str,Job]:
        u=f"https://boards.greenhouse.io/example/jobs/{jid}"
        return label,Job(source_site="greenhouse",source_job_id=str(jid),canonical_url=u,apply_url=u,title=title,company="Example Health",location_raw=location,remote_status="remote",employment_type=employment,posted_at=now_iso(),description=desc,raw={"_board":"example"})
    samples=[
        gh("good","Patient Enrollment Specialist","Healthcare company. Manage remote patient monitoring enrollment, patient onboarding, HIPAA documentation, workflow handoffs and Excel reporting. Required Qualifications: 2 years relevant experience.",jid=1001),
        gh("stretch","Healthcare Data Analyst","Required Qualifications: 2+ years of data analytics experience. SQL, HL7, CCDA and ADT required. Healthcare clinical data quality and interoperability analysis.",jid=1002),
        gh("license","Healthcare Quality Specialist","Required Qualifications: Active RN license required. Healthcare quality improvement and documentation audits.",jid=1003),
        gh("hybrid","Patient Access Specialist","This is not a fully remote position. Hybrid work model with three days onsite.",jid=1004),
        gh("engineer","Senior AI Engineer - AI Platform","Required Qualifications: 5+ years software engineering, Python, AWS and distributed systems.",jid=1005),
        gh("finance","Financial Data & Systems Analyst","Required Qualifications: Bachelor's degree in accounting or finance and 4+ years financial analysis/accounting experience. Build dashboards, Power BI and SQL financial models.",jid=1006),
        gh("do_bug","Technical Program Manager","What we need to see: 8+ years of program management. Do the right thing and coordinate teams.",jid=1007),
        gh("dataops","Data Operations Specialist","Review operational data, perform data quality checks, investigate discrepancies, use Excel and data validation. Required Qualifications: 1+ year experience.",jid=1008),
        gh("remote_cond","Patient Enrollment Specialist","This role is open to remote candidates across the United States. Team members located within 40 miles of our Scottsdale headquarters are expected to work onsite four days per week. Required Qualifications: 2 years healthcare enrollment experience.",jid=1009),
        gh("remote_excl","Patient Enrollment Specialist","This is a fully remote United States role. We are not considering candidates residing in TX, CA, or NY. Required Qualifications: 2 years healthcare enrollment experience.",jid=1010),
        gh("contract","Patient Enrollment Specialist","Healthcare patient enrollment and documentation. Required Qualifications: 2 years relevant experience.",employment="Contract",jid=1011),
        gh("philippines","Patient Enrollment Specialist","Healthcare patient enrollment and documentation.",location="Remote - Philippines",jid=1012),
        gh("offshore_title","Tier 2 Member Support Agent (Offshore - Philippines)","Member support and documentation.",location="Remote - USA",employment="Contract",jid=1015),
        gh("cnm","Women's Health Specialist - Certified Nurse Midwife (CNM)","Required Qualifications: active CNM credential and clinical practice.",jid=1013),
        gh("supervisor","Patient Access Supervisor","Supervise a team of patient access specialists. Required Qualifications: 3 years patient access and prior team supervision.",jid=1014),
        ("empty_credential",Job(source_site="remotelanders",source_job_id="cred",canonical_url="https://remotelanders.com/jobs/credentialing-associate",apply_url="https://jobs.lever.co/example/abc123",title="Credentialing Associate",company="Example Health",location_raw="Remote - US",remote_status="remote",employment_type="Full-time",posted_at=now_iso(),description="",raw={})),
        ("empty_enrollment",Job(source_site="remotelanders",source_job_id="enr",canonical_url="https://remotelanders.com/jobs/enrollment-specialist",apply_url="https://jobs.ashbyhq.com/example/00000000-0000-0000-0000-000000000000",title="Enrollment Specialist",company="Example Health",location_raw="Remote - US",remote_status="remote",employment_type="Full-time",posted_at=now_iso(),description="",raw={})),
    ]
    got={}
    for label,j in samples:
        setattr(j,"_mode","deep"); score_job(j,strategy,config.get("candidate",{})); got[label]=j
        print(f"{label:16s} {j.recommendation:18s} R={j.relevance_score:>5.1f} Q={j.qualification_score:>5.1f} L={j.landing_score:>5.1f} {j.title}")
    assert got["good"].recommendation in {"APPLY_NOW","APPLY_VOLUME"}
    assert got["stretch"].recommendation not in {"APPLY_NOW","APPLY_VOLUME"}
    assert got["license"].recommendation=="SKIP_HARD_GATE"
    assert got["hybrid"].recommendation=="SKIP_HARD_GATE"
    assert got["engineer"].recommendation=="OUT_OF_SCOPE"
    assert got["finance"].recommendation=="OUT_OF_SCOPE"
    assert not any("required credential not documented in resume: DO"==x for x in got["do_bug"].hard_reject_reasons)
    assert got["dataops"].recommendation in {"APPLY_NOW","APPLY_VOLUME","REVIEW","HIGH_VALUE_STRETCH"}
    assert not phrase_present("SIS","analysis") and not phrase_present("Lean","clean")
    assert got["remote_cond"].remote_gate=="pass" and got["remote_excl"].remote_gate=="reject"
    assert got["contract"].recommendation=="CONTRACT_REVIEW"
    assert got["philippines"].recommendation=="SKIP_HARD_GATE"
    assert got["offshore_title"].remote_gate=="reject" and got["offshore_title"].recommendation=="SKIP_HARD_GATE"
    assert got["cnm"].recommendation=="OUT_OF_SCOPE"
    assert any("people-management" in x for x in got["supervisor"].requirement_gaps) and got["supervisor"].recommendation not in {"APPLY_NOW","APPLY_VOLUME","HIGH_VALUE_STRETCH"}
    # Critical v2.1 recall regression: empty broad-source descriptions must still enter the
    # candidate universe so their canonical ATS pages can be fetched on the same/next deep run.
    assert got["empty_credential"].relevance_score>=65 and got["empty_credential"].recommendation=="VERIFY_SOURCE"
    assert got["empty_enrollment"].relevance_score>=65 and got["empty_enrollment"].recommendation=="VERIFY_SOURCE"
    print("SELF-TEST PASSED")
    return 0

def versioning_test(config:dict[str,Any],strategy:dict[str,Any])->int:
    fd,path=tempfile.mkstemp(prefix="jobbot_v2_",suffix=".sqlite3");
    import os; os.close(fd); Path(path).unlink(missing_ok=True)
    try:
        s=PrecisionStore(Path(path)); rid=s.begin_run(now_iso(),"deep")
        j=Job(source_site="greenhouse",source_job_id="1001",canonical_url="https://boards.greenhouse.io/acme/jobs/1001",apply_url="https://boards.greenhouse.io/acme/jobs/1001",title="Patient Enrollment Specialist",company="Acme Health",location_raw="Remote - United States",remote_status="remote",salary_text="$55,000 - $65,000",salary_min=55000,salary_max=65000,posted_at=now_iso(),description="Healthcare patient enrollment, RPM onboarding, HIPAA documentation and Excel. Required Qualifications: 2 years relevant experience.",raw={"_board":"acme"}); setattr(j,"_mode","deep"); score_job(j,strategy,config.get("candidate",{}))
        a=s.upsert(j,rid); b=s.upsert(j,rid)
        j2=Job(**{k:getattr(j,k) for k in Job.__dataclass_fields__ if hasattr(j,k) and k not in {"raw"}}); j2.raw={"_board":"acme"}; j2.salary_text="$60,000 - $70,000"; j2.salary_min=60000; j2.salary_max=70000; j2.description=j.description+" Power BI is preferred."; setattr(j2,"_mode","deep"); score_job(j2,strategy,config.get("candidate",{})); d=s.upsert(j2,rid)
        versions=s.versions(s.resolve_job_id(j2)); assert a=="new" and b=="unchanged" and d=="updated" and len(versions)==2
        diff=json.loads(versions[0]["diff_json"]); assert "salary_text" in diff and "description" in diff
        low=Job(source_site="jobicy",source_job_id="mirror",canonical_url=j.canonical_url,apply_url=j.apply_url,title=j.title,company=j.company,location_raw=j.location_raw,remote_status="remote",salary_text="$1",description="Bad mirror text",posted_at=now_iso(),raw={}); setattr(low,"_mode","deep"); score_job(low,strategy,config.get("candidate",{})); st=s.upsert(low,rid)
        row=s.conn.execute("SELECT * FROM jobs WHERE job_id=?",(s.resolve_job_id(j),)).fetchone(); assert row["salary_text"]=="$60,000 - $70,000" and row["canonical_source_site"]=="greenhouse" and st=="unchanged"
        # Two separate requisitions with the same employer/title/location must remain distinct.
        other=Job(source_site="greenhouse",source_job_id="2002",canonical_url="https://boards.greenhouse.io/acme/jobs/2002",apply_url="https://boards.greenhouse.io/acme/jobs/2002",title=j.title,company=j.company,location_raw=j.location_raw,remote_status="remote",posted_at=now_iso(),description="Different requisition for evening-shift patient enrollment. Required Qualifications: 2 years relevant experience.",raw={"_board":"acme"}); setattr(other,"_mode","deep"); score_job(other,strategy,config.get("candidate",{})); assert s.upsert(other,rid)=="new"; assert s.resolve_job_id(other)!=s.resolve_job_id(j)
        # Complete-ATS disappearance requires two full misses before closure, then reappearance reopens/version-diffs.
        assert s.reconcile_complete_board("greenhouse","acme",set(),rid,2)==0
        assert s.reconcile_complete_board("greenhouse","acme",set(),rid,2)==2
        row=s.conn.execute("SELECT is_active,posting_status FROM jobs WHERE job_id=?",(s.resolve_job_id(j),)).fetchone(); assert row["is_active"]==0 and row["posting_status"]=="closed"
        st2=s.upsert(j2,rid); row=s.conn.execute("SELECT is_active FROM jobs WHERE job_id=?",(s.resolve_job_id(j),)).fetchone(); assert row["is_active"]==1 and st2=="updated"
        s.record_run(rid,{},5,1,1,2,2,1); s.close(); print("VERSIONING TEST PASSED"); return 0
    finally:
        for q in (Path(path),Path(path+"-wal"),Path(path+"-shm")): q.unlink(missing_ok=True)


def throughput_test(config:dict[str,Any],strategy:dict[str,Any])->int:
    fd,path=tempfile.mkstemp(prefix="jobbot_throughput_",suffix=".sqlite3"); import os; os.close(fd); Path(path).unlink(missing_ok=True)
    try:
        s=PrecisionStore(Path(path)); rid=s.begin_run(now_iso(),"deep")
        for i in range(25):
            title="Patient Enrollment Specialist" if i%2==0 else "Patient Access Specialist"
            j=Job(source_site="greenhouse",source_job_id=str(i),canonical_url=f"https://boards.greenhouse.io/healthco/jobs/{10000+i}",apply_url=f"https://boards.greenhouse.io/healthco/jobs/{10000+i}",title=title,company=f"HealthCo {i//2}",location_raw="Remote - United States",remote_status="remote",employment_type="Full-time",posted_at=now_iso(),description="Healthcare patient enrollment onboarding HIPAA documentation workflow. Required Qualifications: 2 years relevant experience.",raw={"_board":"healthco"}); setattr(j,"_mode","deep"); score_job(j,strategy,config.get("candidate",{})); s.upsert(j,rid)
        picked=select_daily_plan(s.rows(),strategy,15); assert len(picked)==15, len(picked); s.close(); print("THROUGHPUT TEST PASSED — 15/15 selected without lowering qualification thresholds"); return 0
    finally:
        for q in (Path(path),Path(path+"-wal"),Path(path+"-shm")): q.unlink(missing_ok=True)


def integrity_check(store:PrecisionStore)->tuple[bool,list[str]]:
    problems=[]
    orphan=store.conn.execute("SELECT COUNT(*) n FROM occurrences o LEFT JOIN jobs j ON j.job_id=o.job_id WHERE j.job_id IS NULL").fetchone()["n"]
    if orphan: problems.append(f"orphan occurrences: {orphan}")
    orphanv=store.conn.execute("SELECT COUNT(*) n FROM job_versions v LEFT JOIN jobs j ON j.job_id=v.job_id WHERE j.job_id IS NULL").fetchone()["n"]
    if orphanv: problems.append(f"orphan versions: {orphanv}")
    dupv=store.conn.execute("SELECT COUNT(*) n FROM (SELECT job_id,version_no,COUNT(*) c FROM job_versions GROUP BY job_id,version_no HAVING c>1)").fetchone()["n"]
    if dupv: problems.append(f"duplicate job versions: {dupv}")
    nov=store.conn.execute("SELECT COUNT(*) n FROM jobs j WHERE NOT EXISTS (SELECT 1 FROM job_versions v WHERE v.job_id=j.job_id)").fetchone()["n"]
    if nov: problems.append(f"jobs without version history: {nov}")
    badactive=store.conn.execute("SELECT COUNT(*) n FROM jobs WHERE is_active=0 AND posting_status NOT IN ('closed','') AND posting_status IS NOT NULL").fetchone()["n"]
    if badactive: problems.append(f"inactive jobs with conflicting posting_status: {badactive}")
    return not problems,problems


def print_job_diff(store:PrecisionStore,job_id:str)->int:
    rows=store.versions(job_id)
    if not rows: print("No version history for",job_id); return 1
    print(f"{job_id}: {len(rows)} version(s)")
    for r in rows[:10]:
        print(f"\nVersion {r['version_no']} · {r['reason']} · {r['observed_at']} · source={r['source_site']}")
        try:d=json.loads(r["diff_json"] or "{}")
        except Exception:d={}
        print(json.dumps(d,ensure_ascii=False,indent=2)[:12000])
    return 0


def build_retrieval_audit(store:PrecisionStore,out:Path)->Path:
    """Explain exactly where the retrieved market went; this prevents opaque 'only N Apply Now' failures."""
    def scalar(sql:str,args:tuple=())->int:
        r=store.conn.execute(sql,args).fetchone(); return int(r[0] or 0) if r else 0
    def groups(col:str,where:str="1=1",limit:int=30)->list[tuple[str,int]]:
        return [(clean_text(r[0]) or "(blank)",int(r[1] or 0)) for r in store.conn.execute(f"SELECT {col},COUNT(*) c FROM jobs WHERE {where} GROUP BY {col} ORDER BY c DESC LIMIT ?",(limit,)).fetchall()]
    total=scalar("SELECT COUNT(*) FROM jobs"); active=scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1")
    candidate=scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1 AND COALESCE(relevance_score,0)>=65")
    relevant=scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1 AND COALESCE(relevance_score,0)>=65 AND recommendation NOT IN ('OUT_OF_SCOPE','SKIP_HARD_GATE','SKIP_SOURCE')")
    qualified=scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1 AND COALESCE(qualification_score,0)>=68")
    reservoir=scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation IN ('APPLY_NOW','APPLY_VOLUME','HIGH_VALUE_STRETCH') AND application_status NOT IN ('applied','screen','interview','final_interview','offer','rejected')")
    blank=scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1 AND LENGTH(TRIM(COALESCE(description,'')))<120")
    remote_pass=scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1 AND remote_gate='pass'")
    remote_reject=scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1 AND remote_gate='reject'")
    verify=scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='VERIFY_SOURCE'")
    contract=scalar("SELECT COUNT(*) FROM jobs WHERE is_active=1 AND recommendation='CONTRACT_REVIEW'")
    applied=scalar("SELECT COUNT(*) FROM jobs WHERE application_status IN ('applied','screen','interview','final_interview','offer','rejected')")
    big3={p:scalar("SELECT COUNT(DISTINCT job_id) FROM occurrences WHERE source_site=?",(p,)) for p in ('linkedin','indeed','glassdoor')}
    due={p:scalar("SELECT COUNT(*) FROM coverage_segments WHERE platform=? AND status='due'",(p,)) for p in ('linkedin','indeed','glassdoor')}
    lines=["# Retrieval / Research Audit","",f"Generated: {now_iso()}","", "## Funnel",
           f"- Canonical jobs stored: **{total:,}**",f"- Currently active: **{active:,}**",f"- Active descriptions missing/too short: **{blank:,}**",f"- Candidate universe (relevance ≥65): **{candidate:,}**",f"- Relevant after role gates: **{relevant:,}**",f"- Qualification coverage ≥68: **{qualified:,}**",f"- Remote confirmed / rejected: **{remote_pass:,} / {remote_reject:,}**",f"- Waiting for canonical/source verification: **{verify:,}**",f"- Contract review: **{contract:,}**",f"- Primary unapplied application reservoir: **{reservoir:,}**",f"- Already in application funnel: **{applied:,}**","",
           "## Big-3 assisted coverage",f"- LinkedIn jobs captured: **{big3['linkedin']:,}** · due segments: **{due['linkedin']:,}**",f"- Indeed jobs captured: **{big3['indeed']:,}** · due segments: **{due['indeed']:,}**",f"- Glassdoor jobs captured: **{big3['glassdoor']:,}** · due segments: **{due['glassdoor']:,}**","",
           "> A low final reservoir can come from incomplete Big-3 coverage, empty discovery-feed descriptions, canonical-verification backlog, or true qualification/employment gates. This report keeps those causes separate.",""]
    if scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='browser_search_tasks'"):
        task_total=scalar("SELECT COUNT(*) FROM browser_search_tasks")
        task_counts={status:scalar(f"SELECT COUNT(*) FROM browser_search_tasks WHERE status='{status}'") for status in ("exhausted","incomplete","challenged","auth_required","failed","queued","running")}
        run_counts={status:scalar(f"SELECT COUNT(*) FROM browser_runs WHERE status='{status}'") for status in ("completed","partial","stopped","running")}
        lines += ["## Browser task coverage","",f"- Browser runs: **{scalar('SELECT COUNT(*) FROM browser_runs'):,}** (completed {run_counts['completed']:,}, partial {run_counts['partial']:,}, stopped {run_counts['stopped']:,}, running {run_counts['running']:,})",f"- Search tasks total: **{task_total:,}**",f"- Exhausted: **{task_counts['exhausted']:,}**",f"- Incomplete / safety stop: **{task_counts['incomplete']:,}**",f"- Challenged: **{task_counts['challenged']:,}**",f"- Auth required: **{task_counts['auth_required']:,}**",f"- Failed: **{task_counts['failed']:,}**",f"- Queued / running: **{task_counts['queued']:,} / {task_counts['running']:,}**","", "| Platform | Queries | Exhausted | Incomplete | Challenged | Auth required | Failed | Results | Details read | Unique jobs | Duplicate sightings |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        has_results=scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='search_task_results'")
        for platform in ("linkedin","indeed","glassdoor"):
            row=store.conn.execute("""SELECT COUNT(*) queries, SUM(status='exhausted') exhausted, SUM(status='incomplete') incomplete,
              SUM(status='challenged') challenged, SUM(status='auth_required') auth_required, SUM(status='failed') failed,
              COALESCE(SUM(results_seen),0) results, COALESCE(SUM(detail_count_read),0) details,
              COALESCE(SUM(duplicate_sightings),0) duplicates FROM browser_search_tasks WHERE platform=?""",(platform,)).fetchone()
            unique=scalar("SELECT COUNT(DISTINCT r.canonical_job_id) FROM search_task_results r JOIN browser_search_tasks t ON t.task_id=r.task_id WHERE t.platform=? AND r.canonical_job_id IS NOT NULL",(platform,)) if has_results else 0
            lines.append(f"| {platform} | {row['queries'] or 0} | {row['exhausted'] or 0} | {row['incomplete'] or 0} | {row['challenged'] or 0} | {row['auth_required'] or 0} | {row['failed'] or 0} | {row['results'] or 0} | {row['details'] or 0} | {unique} | {row['duplicates'] or 0} |")
        lines.append("")
    for heading,col,where in [
        ("Recommendation distribution","recommendation","is_active=1"),
        ("Canonical source distribution","canonical_source_site","is_active=1"),
        ("Source verification","source_verification","is_active=1"),
        ("Employment arrangement","employment_class","is_active=1 AND COALESCE(relevance_score,0)>=65"),
        ("Search/profile family","search_profile","is_active=1 AND COALESCE(relevance_score,0)>=65"),
    ]:
        lines += [f"## {heading}",""]+[f"- {k}: **{v:,}**" for k,v in groups(col,where)]+[""]
    lines += ["## Missing descriptions by canonical source",""]
    rows=store.conn.execute("SELECT canonical_source_site,COUNT(*) c FROM jobs WHERE is_active=1 AND LENGTH(TRIM(COALESCE(description,'')))<120 GROUP BY canonical_source_site ORDER BY c DESC").fetchall()
    lines += [f"- {clean_text(r[0]) or '(blank)'}: **{int(r[1]):,}**" for r in rows] or ["- None"]
    lines += ["","## Interpretation","", "- Discovery is intentionally high-recall and append-only.","- `VERIFY_SOURCE` is not a rejection; it means the role needs canonical employer/ATS confirmation before entering the primary application reservoir.","- Contract/part-time/foreign/clinical/seniority gates remain separate so increasing recall cannot silently weaken application quality.","- LinkedIn/Indeed/Glassdoor counts stay at zero until their assisted coverage searches are actually reviewed/captured; generating coverage links alone is not counted as retrieval.",""]
    out.mkdir(parents=True,exist_ok=True)
    path=out/"retrieval_audit.md"
    markdown="\n".join(lines)+"\n"
    path.write_text(markdown,encoding="utf-8")
    (out/"retrieval_audit.html").write_text(
        "<!doctype html><meta charset='utf-8'><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'none'\"><title>JobBot Retrieval Audit</title><style>body{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;max-width:1200px;margin:24px auto;padding:0 18px;background:#f6f8fb;color:#172033}h1{font-family:system-ui,sans-serif}main{background:#fff;border:1px solid #e2e7ef;border-radius:14px;padding:18px;overflow:auto}</style><h1>JobBot Retrieval Audit</h1><main>"+c.html.escape(markdown)+"</main>",
        encoding="utf-8",
    )
    return path


def coverage_summary(store:PrecisionStore,platform:str="")->str:
    rows=store.coverage_rows(platform); by={}; due=0
    for r in rows:
        by[r["platform"]]=by.get(r["platform"],0)+1
        if r["status"]=="due": due+=1
    return f"Coverage segments: {len(rows)} | due/unconfirmed: {due} | "+", ".join(f"{k}={v}" for k,v in sorted(by.items()))



def candidate_readiness(config:dict[str,Any])->tuple[list[str],list[str]]:
    """Return (warnings, confirmations) for candidate settings that materially affect gates/ranking."""
    cand=config.get("candidate",{})
    warn=[]; ok=[]
    wa=clean_text(cand.get("work_authorization") or "unknown").lower()
    if wa in {"", "unknown", "unspecified"}:
        warn.append("work_authorization is unknown: postings that require unrestricted US work authorization/no sponsorship are sent to REVIEW instead of APPLY")
    else: ok.append(f"work authorization configured: {wa}")
    floor=float(cand.get("minimum_salary_annual",0) or 0)
    if floor<=0: warn.append("minimum_salary_annual is 0: compensation will rank jobs but will not hard-reject low-pay roles")
    else: ok.append(f"minimum salary floor configured: ${floor:,.0f}/yr")
    mt=float(cand.get("max_travel_percent",-1) if cand.get("max_travel_percent",-1) is not None else -1)
    if mt<0: warn.append("max_travel_percent is not set: travel is extracted and displayed but is not a hard gate")
    else: ok.append(f"maximum travel configured: {mt:g}%")
    if cand.get("remote_only",False): ok.append("remote-only hard gate enabled")
    else: warn.append("remote_only is false; this conflicts with the stated search objective")
    if not clean_text(cand.get("state")): warn.append("candidate.state is blank; state-eligibility checks will be weaker")
    else: ok.append(f"state eligibility anchor: {cand.get('state')}")
    return warn,ok


def print_candidate_readiness(config:dict[str,Any])->int:
    warn,ok=candidate_readiness(config)
    print("Candidate configuration readiness")
    for x in ok: print("  OK   ",x)
    for x in warn: print("  WARN ",x)
    print(f"\n{len(ok)} configured item(s), {len(warn)} decision-quality warning(s). Warnings do not stop retrieval.")
    return 0


def build_coverage_session(store:PrecisionStore,out:Path,platform:str="",limit:int=12)->Path:
    """Build a manageable user-assisted coverage work session without claiming silent crawling."""
    limit=max(1,min(50,int(limit or 12)))
    rows=store.coverage_rows(platform,due_only=True)[:limit]
    path=out/"coverage_session.html"; out.mkdir(parents=True,exist_ok=True)
    body=["<!doctype html><meta charset='utf-8'><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'none'\"><meta name='referrer' content='no-referrer'><title>Coverage Session</title><style>body{font-family:system-ui;margin:24px;background:#f6f8fb;color:#172033;max-width:1100px}article{background:#fff;border:1px solid #e1e5eb;border-radius:14px;padding:15px;margin:12px 0}.meta{color:#596579}.id{font-family:monospace}a{color:#1359b2;font-weight:650}.cmd{background:#f1f4f8;border-radius:8px;padding:8px;overflow-wrap:anywhere}</style>",f"<h1>Assisted Search Coverage Session</h1><p>{len(rows)} due segment(s) queued. Open one, review the reachable results normally, capture useful jobs, then mark the segment complete. The ledger remembers completed/overlapping coverage.</p>"]
    if not rows: body.append("<p><strong>No due coverage segments for this selection.</strong></p>")
    for i,r in enumerate(rows,1):
        u=c.safe_output_url(r["search_url"]); sid=c.html.escape(r["segment_id"]); plat=c.html.escape(r["platform"]); q=c.html.escape(r["query_text"]); prof=c.html.escape(r["search_profile"])
        body.append(f"<article><div><strong>{i}. {plat.upper()} · {r['window_days']}d</strong></div><div class='meta'>{prof}</div><p>{q}</p><p class='meta'>{c.html.escape(r['notes'] or '')}</p><p><a target='_blank' rel='noopener noreferrer' referrerpolicy='no-referrer' href='{c.html.escape(u,quote=True)}'>Open this search</a></p><div class='cmd'>Capture a useful posting: <code>python jobbot.py capture --platform {plat}</code><br>When fully reviewed: <code>python jobbot.py coverage-done {sid}</code><br>If the result set is capped/too broad: <code>python jobbot.py coverage-split {sid}</code></div></article>")
    path.write_text("".join(body),encoding="utf-8"); return path


def build_updates_html(store:PrecisionStore,out:Path)->Path:
    """Human-readable local HTML change feed. Full immutable history remains in SQLite."""
    rows=store.conn.execute("""SELECT j.job_id,j.change_status,j.last_changed_at,j.update_count,j.title,j.company,j.recommendation,j.is_active,j.apply_url,j.canonical_url,
      v.version_no,v.reason,v.observed_at,v.diff_json FROM jobs j LEFT JOIN job_versions v ON v.version_id=(SELECT vv.version_id FROM job_versions vv WHERE vv.job_id=j.job_id ORDER BY vv.version_no DESC LIMIT 1)
      WHERE j.change_status IN ('NEW','UPDATED') AND (j.change_ack_at IS NULL OR j.change_ack_at<j.last_changed_at) ORDER BY j.last_changed_at DESC""").fetchall()
    body=["<!doctype html><meta charset='utf-8'><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'none'\"><meta name='referrer' content='no-referrer'><title>Job Changes</title><style>body{font-family:system-ui;margin:24px;background:#f6f8fb;color:#172033;max-width:1100px}article{background:white;border:1px solid #e2e7ef;border-radius:14px;padding:16px;margin:12px 0}.new{border-left:6px solid #15965a}.updated{border-left:6px solid #377bd8}.plus{color:#176c42}.minus{color:#a12b2b}code{overflow-wrap:anywhere}a{color:#1359b2}</style>",f"<h1>Unacknowledged New / Updated Jobs</h1><p>{len(rows)} item(s). Acknowledging clears the current notification flag only; immutable versions and diffs remain in the database.</p>"]
    for r in rows:
        try: d=json.loads(r["diff_json"] or "{}")
        except Exception: d={}
        cls="new" if r["change_status"]=="NEW" else "updated"; url=c.safe_output_url(r["apply_url"] or r["canonical_url"])
        body.append(f"<article class='{cls}'><h2>{c.html.escape(r['change_status'])} — {c.html.escape(r['title'])} — {c.html.escape(r['company'])}</h2><p><code>{c.html.escape(r['job_id'])}</code> · version {r['version_no']} · {c.html.escape(r['observed_at'] or '')} · {c.html.escape(r['recommendation'] or '')}</p>")
        if not d: body.append("<p>No field-level diff available for this version.</p>")
        for k,v in d.items():
            if k=="description" and isinstance(v,dict):
                body.append(f"<p><strong>Description:</strong> {v.get('old_chars',0)} → {v.get('new_chars',0)} chars</p>")
                for x in v.get("added",[])[:8]: body.append(f"<div class='plus'>+ {c.html.escape(clean_text(x)[:500])}</div>")
                for x in v.get("removed",[])[:8]: body.append(f"<div class='minus'>− {c.html.escape(clean_text(x)[:500])}</div>")
            elif isinstance(v,dict): body.append(f"<p><strong>{c.html.escape(str(k))}:</strong> {c.html.escape(str(v.get('old')))} → {c.html.escape(str(v.get('new')))}</p>")
        if url: body.append(f"<p><a target='_blank' rel='noopener noreferrer' referrerpolicy='no-referrer' href='{c.html.escape(url,quote=True)}'>Open current posting</a></p>")
        body.append("</article>")
    path=out/"updates.html"; path.write_text("".join(body),encoding="utf-8"); return path

def doctor(config:dict[str,Any],strategy:dict[str,Any],db:Path)->int:
    rc=0
    print("[1/6] precision self-test"); rc|=self_test(config,strategy)
    print("[2/6] security check"); rc|=c.security_check(config)
    print("[3/6] immutable version/diff test"); rc|=versioning_test(config,strategy)
    print("[4/6] 15/day throughput test"); rc|=throughput_test(config,strategy)
    print("[5/6] database integrity")
    s=PrecisionStore(db); ok,problems=integrity_check(s); s.close()
    if ok: print("INTEGRITY CHECK PASSED")
    else:
        rc=1
        for x in problems: print("INTEGRITY ERROR:",x)
    print("[6/6] candidate decision-readiness")
    print_candidate_readiness(config)
    print("DOCTOR PASSED" if rc==0 else "DOCTOR FAILED")
    return rc

def main()->int:
    ap=argparse.ArgumentParser(description="Remote Career Job Search Automation v3.1.0 — recall-first canonical verification + append-only ledger + precision + throughput"); ap.add_argument("--config",default="config.toml"); sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("run",help="Retrieve every reachable job inside configured automatic-source boundaries, version changes, qualify, score, and export"); p.add_argument("--mode",choices=["fast","deep"],default="fast")
    sub.add_parser("stats"); sub.add_parser("progress"); sub.add_parser("audit",help="Explain retrieval volume, filtering, verification backlog and Big-3 coverage"); sub.add_parser("candidate-check"); sub.add_parser("self-test"); sub.add_parser("security-check"); sub.add_parser("versioning-test"); sub.add_parser("throughput-test"); sub.add_parser("doctor"); sub.add_parser("integrity-check")
    p=sub.add_parser("capture",help="Safely capture one supplemental public job page from user-assisted browsing (Big-3 disabled)"); p.add_argument("--platform",default="web"); p.add_argument("--url",default="")
    p=sub.add_parser("mark"); p.add_argument("job_id"); p.add_argument("status"); p.add_argument("--notes",default="")
    p=sub.add_parser("mark-batch",help="Mark multiple jobs with one status"); p.add_argument("status"); p.add_argument("job_ids",nargs="+")
    p=sub.add_parser("daily-plan"); p.add_argument("--target",type=int,default=0)
    p=sub.add_parser("prepare",help="Create an application packet for one job"); p.add_argument("job_id")
    p=sub.add_parser("prepare-daily",help="Create packets for today's application slate"); p.add_argument("--target",type=int,default=0)
    p=sub.add_parser("regression-test"); p.add_argument("html_file")
    p=sub.add_parser("diff",help="Show immutable version history and field diffs for one canonical job"); p.add_argument("job_id")
    p=sub.add_parser("rescore",help="Re-run current canonical ledger through the latest strategy without pretending employer postings changed"); p.add_argument("--mode",choices=["fast","deep"],default="deep")
    sub.add_parser("updates",help="Export/print currently unacknowledged NEW/UPDATED postings"); sub.add_parser("ack-updates",help="Acknowledge the current new/updated change feed"); sub.add_parser("funnel-report",help="Analyze application→screen→interview→offer conversion and empirical title-family learning")
    p=sub.add_parser("coverage",help="Show assisted LinkedIn/Indeed/Glassdoor coverage state"); p.add_argument("--platform",default="")
    p=sub.add_parser("coverage-open-next",help="Open the next due user-assisted search segment"); p.add_argument("--platform",choices=["linkedin","indeed","glassdoor"],default="linkedin")
    p=sub.add_parser("coverage-done",help="Mark one fully-reviewed search segment complete"); p.add_argument("segment_id"); p.add_argument("--notes",default="")
    p=sub.add_parser("coverage-split",help="Split a capped/too-broad Boolean segment into smaller child searches"); p.add_argument("segment_id")
    p=sub.add_parser("coverage-session",help="Build/open a manageable batch of due LinkedIn/Indeed/Glassdoor assisted searches"); p.add_argument("--platform",choices=["all","linkedin","indeed","glassdoor"],default="all"); p.add_argument("--limit",type=int,default=12)
    sub.add_parser("open"); sub.add_parser("open-searches"); sub.add_parser("open-daily"); sub.add_parser("open-updates")
    args=ap.parse_args(); cp=Path(args.config).resolve(); base=cp.parent; config=load_toml(cp); config["_base"]=str(base); strategy=load_toml(abs_path(base,config.get("app",{}).get("strategy_file","strategy.toml"))); ac=config.get("app",{}); db=abs_path(base,ac.get("db_path","data/jobs.sqlite3")); out=abs_path(base,ac.get("output_dir","out"))
    if args.cmd=="run": return run_search(config,strategy,args.mode)
    if args.cmd=="self-test": return self_test(config,strategy)
    if args.cmd=="security-check": return c.security_check(config)
    if args.cmd=="versioning-test": return versioning_test(config,strategy)
    if args.cmd=="throughput-test": return throughput_test(config,strategy)
    if args.cmd=="doctor": return doctor(config,strategy,db)
    if args.cmd=="regression-test": return regression_test_from_html(Path(args.html_file),config,strategy)
    if args.cmd=="integrity-check":
        s=PrecisionStore(db); ok,problems=integrity_check(s); s.close()
        if ok: print("INTEGRITY CHECK PASSED"); return 0
        [print("INTEGRITY ERROR:",x) for x in problems]; return 1
    if args.cmd=="stats": s=PrecisionStore(db); print_summary(s); print(progress_summary(s,strategy)); print(coverage_summary(s)); s.close(); return 0
    if args.cmd=="audit":
        s=PrecisionStore(db); path=build_retrieval_audit(s,out); print(path.read_text(encoding="utf-8")); s.close(); return 0
    if args.cmd=="progress": s=PrecisionStore(db); print(progress_summary(s,strategy)); s.close(); return 0
    if args.cmd=="candidate-check": return print_candidate_readiness(config)
    if args.cmd=="daily-plan": s=PrecisionStore(db); picked=build_daily_plan(s,out,strategy,args.target or None); print(f"Daily plan contains {len(picked)} jobs: {out/'daily_apply_plan.md'}"); s.close(); return 0
    if args.cmd=="prepare": s=PrecisionStore(db); path=prepare_packet(s,out,base,args.job_id); s.close(); print(f"Application packet: {path}"); return 0
    if args.cmd=="prepare-daily": s=PrecisionStore(db); paths=prepare_daily_packets(s,out,base,strategy,args.target or None); s.close(); print(f"Prepared {len(paths)} application packets in {out/'application_packets'}"); return 0
    if args.cmd=="mark-batch": s=PrecisionStore(db); [s.mark(j,args.status) for j in args.job_ids]; print(progress_summary(s,strategy)); s.close(); return 0
    if args.cmd=="mark": s=PrecisionStore(db); s.mark(args.job_id,args.status,args.notes); print(progress_summary(s,strategy)); s.close(); return 0
    if args.cmd=="diff": s=PrecisionStore(db); rc=print_job_diff(s,args.job_id); s.close(); return rc
    if args.cmd=="rescore":
        s=PrecisionStore(db); counts=s.rescore_all(strategy,config.get("candidate",{}),args.mode); export_all(s,out,strategy,config,args.mode); print("Rescored",sum(counts.values()),"canonical jobs without creating employer-content versions:",counts); s.close(); return 0
    if args.cmd=="funnel-report":
        s=PrecisionStore(db); path=build_funnel_report(s,out,strategy); print(path); s.close(); return 0
    if args.cmd=="updates":
        s=PrecisionStore(db); export_updates(s,out); n=s.conn.execute("SELECT COUNT(*) n FROM jobs WHERE change_status IN ('NEW','UPDATED') AND (change_ack_at IS NULL OR change_ack_at<last_changed_at)").fetchone()["n"]; s.close(); print(f"Unacknowledged NEW/UPDATED jobs: {n}\n{out/'updates.md'}"); return 0
    if args.cmd=="ack-updates": s=PrecisionStore(db); n=s.acknowledge_changes(); s.close(); print(f"Acknowledged {n} current change flag(s). Future employer changes will be flagged again."); return 0
    if args.cmd=="coverage":
        s=PrecisionStore(db); seg=make_coverage_segments(strategy,config,"deep"); s.sync_coverage_segments(seg); build_coverage_dashboard(s,out); print(coverage_summary(s,args.platform));
        for r in s.coverage_rows(args.platform,due_only=True)[:20]: print(f"  DUE {r['segment_id']} {r['platform']} {r['window_days']}d {r['query_text']}")
        s.close(); return 0
    if args.cmd=="coverage-session":
        s=PrecisionStore(db); s.sync_coverage_segments(make_coverage_segments(strategy,config,"deep")); path=build_coverage_session(s,out,"" if args.platform=="all" else args.platform,args.limit); s.close(); webbrowser.open(path.resolve().as_uri()); print(f"Coverage session: {path}"); return 0
    if args.cmd=="coverage-open-next":
        s=PrecisionStore(db); s.sync_coverage_segments(make_coverage_segments(strategy,config,"deep")); rows=s.coverage_rows(args.platform,due_only=True)
        if not rows: print(f"No due {args.platform} coverage segments right now."); s.close(); return 0
        r=rows[0]; s.coverage_opened(r["segment_id"]); webbrowser.open(r["search_url"]); print(f"Opened {r['segment_id']}\nQuery: {r['query_text']}\nAfter completely reviewing this result segment: python jobbot.py coverage-done {r['segment_id']}"); s.close(); return 0
    if args.cmd=="coverage-split":
        s=PrecisionStore(db); ids=s.split_coverage_segment(args.segment_id); build_coverage_dashboard(s,out); s.close(); print("Split",args.segment_id,"into",", ".join(ids)); return 0
    if args.cmd=="coverage-done": s=PrecisionStore(db); s.coverage_done(args.segment_id,args.notes); print("Marked coverage complete:",args.segment_id); s.close(); return 0
    if args.cmd=="capture":
        j=c.browser_capture(config,strategy,args.platform,args.url); s=PrecisionStore(db); rid=s.begin_run(now_iso(),"deep"); st=s.upsert(j,rid); s.record_run(rid,{"manual_capture":{"ok":True,"count":1}},1,1,1 if st=="new" else 0,1 if st=="updated" else 0,1 if st=="unchanged" else 0,0); s.sync_coverage_segments(make_coverage_segments(strategy,config,"deep")); export_all(s,out,strategy,config,"deep"); jid=s.resolve_job_id(j); s.close(); print(f"Captured {jid}: {j.title} — {j.company} | {j.recommendation} | ledger event={st}"); return 0
    if args.cmd=="open":
        target=out/"jobs.html"
        if not target.exists(): print("Run a search first: python jobbot.py run --mode deep"); return 1
        webbrowser.open(target.resolve().as_uri()); return 0
    if args.cmd=="open-searches":
        s=PrecisionStore(db); s.sync_coverage_segments(make_coverage_segments(strategy,config,"deep")); build_coverage_dashboard(s,out); s.close(); webbrowser.open((out/"coverage.html").resolve().as_uri()); return 0
    if args.cmd=="open-daily":
        target=out/"daily_apply_plan.html"
        if not target.exists(): s=PrecisionStore(db); build_daily_plan(s,out,strategy); s.close()
        webbrowser.open(target.resolve().as_uri()); return 0
    if args.cmd=="open-updates":
        s=PrecisionStore(db); export_updates(s,out); s.close(); webbrowser.open((out/"updates.html").resolve().as_uri()); return 0
    return 2

if __name__=="__main__":
    raise SystemExit(main())
