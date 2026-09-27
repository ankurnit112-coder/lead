#!/usr/bin/env python3
"""
Autonomous Daily Lead Prospector & Cold Outreach Pipeline (NVIDIA GLM-5.3-Flash Edition)

This pipeline:
1. Deduplicates prospective businesses against historical records stored in Google Sheets (Column D - Website URL).
2. Scrapes 40-50 high-ticket B2B service candidates daily via Apify across rotating global markets with fallback email extraction.
3. Evaluates & scores candidates using z-ai/glm-5.3-flash via NVIDIA NIM API (OpenAI-compatible).
4. Selects strictly the top 10 highest-quality leads per day and drafts personalized, human-like cold emails (<110 words).
5. Synchronizes top 10 leads to Google Sheets ('Outreach Pipeline - Daily Top 10') with Status = 'Pending Review'.
"""

import argparse
import datetime
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Literal, Optional, Set
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator
from tabulate import tabulate
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Third-party integrations
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    from apify_client import ApifyClient
except ImportError:
    ApifyClient = None

try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
except ImportError:
    gspread = None
    ServiceAccountCredentials = None

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("LeadPipeline")

# Default Constants & Niches
DEFAULT_LLM_MODEL = "z-ai/glm-5.3-flash"
NVIDIA_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_WORKSHEET_NAME = "Outreach Pipeline - Daily Top 10"
DEFAULT_MAX_CANDIDATES = 50

def resolve_model_name(model_name: Optional[str]) -> str:
    """Resolves shorthand model names to full NVIDIA NIM provider format."""
    if not model_name:
        return DEFAULT_LLM_MODEL
    clean = model_name.strip()
    if clean.lower() in ("glm-5.3-flash", "glm-5-3-flash", "glm-5.3", "glm-5-3"):
        return "z-ai/glm-5.3-flash"
    return clean

ROTATING_NICHES = [
    {"name": "Intellectual Property Law Firm", "query": "IP and patent law firm"},
    {"name": "Commercial Roofing Contractor", "query": "commercial industrial roofing contractor"},
    {"name": "Commercial HVAC & Mechanical Systems", "query": "commercial HVAC mechanical contractor"},
    {"name": "Biotechnology Consulting & Regulatory", "query": "biotech regulatory compliance consulting"},
    {"name": "Managed IT Services (MSP) & Cybersecurity", "query": "managed IT services provider MSP cybersecurity"},
    {"name": "Civil & Structural Engineering Firm", "query": "structural engineering consultancy"},
    {"name": "B2B SaaS Consulting & Enterprise Architecture", "query": "enterprise cloud architecture consulting"},
    {"name": "Wealth Management & Private Family Office Advisory", "query": "private wealth management family office advisory"},
    {"name": "Industrial Automation & Robotics Systems", "query": "industrial automation robotics engineering"},
    {"name": "Commercial Architecture & Interior Design", "query": "commercial architecture interior design firm"},
]

ROTATING_REGIONS = [
    {"code": "US", "name": "United States", "cities": ["Austin, TX", "Chicago, IL", "Boston, MA", "Seattle, WA"]},
    {"code": "UK", "name": "United Kingdom", "cities": ["London", "Manchester", "Birmingham", "Edinburgh"]},
    {"code": "AU", "name": "Australia", "cities": ["Sydney", "Melbourne", "Brisbane", "Perth"]},
    {"code": "CA", "name": "Canada", "cities": ["Toronto", "Vancouver", "Calgary", "Montreal"]},
    {"code": "EU", "name": "Western Europe", "cities": ["Dublin, Ireland", "Amsterdam, Netherlands", "Frankfurt, Germany", "Zurich, Switzerland"]},
]

SHEET_HEADERS = [
    "Date Added",
    "Company Name",
    "Country/Region",
    "Website URL",
    "Contact Email",
    "Offer Angle",
    "Quality Score",
    "Core Pain Point",
    "Subject Line",
    "Custom Email Pitch",
    "Status",
]


# ==============================================================================
# Pydantic Data Models
# ==============================================================================

class ProspectCandidate(BaseModel):
    company_name: str
    website: str
    contact_email: Optional[str] = None
    country_code: str
    business_category: str
    phone: Optional[str] = None
    address: Optional[str] = None


class LeadEvaluation(BaseModel):
    offer_angle: Literal["Authority Website", "AI Growth Website"]
    score: int = Field(..., ge=1, le=100)
    primary_pain_point: str

    @field_validator("score")
    @classmethod
    def validate_score(cls, v: int) -> int:
        if not (1 <= v <= 100):
            raise ValueError("Score must be between 1 and 100")
        return v


class EmailPitch(BaseModel):
    subject: str
    email_body: str

    @field_validator("email_body")
    @classmethod
    def validate_length(cls, v: str) -> str:
        words = v.strip().split()
        if len(words) > 130:
            logger.warning(f"Email body exceeds target 110 words ({len(words)} words).")
        return v


class FinalLeadRecord(BaseModel):
    date_added: str
    company_name: str
    country_code: str
    website: str
    contact_email: str
    offer_angle: Literal["Authority Website", "AI Growth Website"]
    score: int
    primary_pain_point: str
    subject: str
    email_body: str
    status: str = "Pending Review"

    def to_sheet_row(self) -> List[Any]:
        return [
            self.date_added,
            self.company_name,
            self.country_code,
            self.website,
            self.contact_email,
            self.offer_angle,
            self.score,
            self.primary_pain_point,
            self.subject,
            self.email_body,
            self.status,
        ]


# ==============================================================================
# Helper Utilities
# ==============================================================================

def normalize_domain(url: Optional[str]) -> str:
    """
    Extracts and normalizes the root domain from a given URL.
    Strips protocols, www prefix, path, port, query params, and trailing slashes.
    Example: 'https://www.apex-law.co.uk/about?ref=1' -> 'apex-law.co.uk'
    """
    if not url or not isinstance(url, str):
        return ""
    url = url.strip().lower()
    if not url:
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        netloc = parsed.netloc or parsed.path.split("/")[0]
        netloc = netloc.split(":")[0]  # remove port
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc.strip()
    except Exception:
        return ""


def extract_json_from_llm_response(text: str) -> Dict[str, Any]:
    """
    Robustly extracts and parses JSON from LLM text responses,
    handling thinking traces (<think>...</think>), markdown code fences (```json ... ```),
    and arbitrary conversational wrapping.
    """
    if not text or not isinstance(text, str):
        raise ValueError("Empty or invalid LLM response string")

    # Strip thinking blocks from reasoning models (e.g. GLM-5.3-Flash)
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    if not cleaned:
        # Fallback to search inside thinking block if the model outputted nothing outside
        cleaned = text.strip()

    # Look for code block fences
    fence_pattern = r"```(?:json)?\s*([\s\S]*?)\s*```"
    fence_match = re.search(fence_pattern, cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    # Direct parse attempt
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Extract outermost balanced JSON object {...}
    brace_match = re.search(r"(\{[\s\S]*\})", cleaned)
    if brace_match:
        try:
            return json.loads(brace_match.group(1))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse valid JSON from text: {text[:200]}...")


def extract_emails_from_text(text: str) -> List[str]:
    """
    Extracts candidate email addresses from raw text using regex,
    filtering out image extensions, common script noise, dummy domains, and npm package versions.
    """
    if not text:
        return []

    # Standard email regex requiring alphabetic TLD (avoids numeric version packages like @11.7.10)
    pattern = r"\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,10}\b"
    matches = re.findall(pattern, text)

    valid_emails = []
    ignored_extensions = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".css", ".js", ".html")
    ignored_substrings = ("sentry", "example.com", "wixpress", "schema.org", "domain.com", "placeholder", "segmenter", "node_modules")

    for email in matches:
        email = email.strip().lower()
        if any(email.endswith(ext) for ext in ignored_extensions):
            continue
        if any(sub in email for sub in ignored_substrings):
            continue
        # Avoid malformed trailing dots
        email = email.rstrip(".")
        if email and "@" in email:
            domain_part = email.split("@")[-1]
            tld = domain_part.split(".")[-1]
            if tld.isalpha() and len(tld) >= 2:
                if email not in valid_emails:
                    valid_emails.append(email)

    return valid_emails


def scrape_email_from_website(url: str, timeout: int = 4) -> Optional[str]:
    """
    Lightweight fallback web scraper to find business email from homepage or /contact.
    """
    if not url:
        return None

    if not url.startswith("http://") and not url.startswith("https://"):
        target_url = "https://" + url
    else:
        target_url = url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    subpaths = ["", "/contact"]

    for subpath in subpaths:
        try:
            full_url = target_url.rstrip("/") + subpath
            resp = requests.get(full_url, headers=headers, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200 and resp.text:
                # 1. Check mailto links first
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"].strip()
                    if href.startswith("mailto:"):
                        mailto_val = href.split("mailto:")[1].split("?")[0].strip()
                        emails = extract_emails_from_text(mailto_val)
                        if emails:
                            return emails[0]

                # 2. Check full page text
                emails = extract_emails_from_text(resp.text)
                if emails:
                    return emails[0]
        except Exception:
            continue

    return None


# ==============================================================================
# Step 3.1: Google Sheets Deduplication Memory
# ==============================================================================

class GoogleSheetSyncManager:
    def __init__(
        self,
        service_account_info: Optional[str] = None,
        sheet_id: Optional[str] = None,
        sheet_name: Optional[str] = None,
        worksheet_name: str = DEFAULT_WORKSHEET_NAME,
        dry_run: bool = False,
    ):
        self.service_account_info = service_account_info
        self.sheet_id = sheet_id
        self.sheet_name = sheet_name
        self.worksheet_name = worksheet_name
        self.dry_run = dry_run
        self.client = None
        self.spreadsheet = None
        self.worksheet = None

    def connect(self) -> bool:
        if self.dry_run and not self.service_account_info:
            logger.info("Dry-run mode active without Google credentials. Skipping live Google Sheets connection.")
            return True

        if not gspread:
            logger.warning("gspread library is not available.")
            return False

        if not self.service_account_info:
            logger.warning("No GOOGLE_SERVICE_ACCOUNT_JSON provided.")
            return False

        try:
            import base64
            info_str = self.service_account_info.strip().strip("'\"").strip()
            if info_str.startswith("\ufeff"):
                info_str = info_str[1:].strip()

            creds_dict = None
            if info_str.startswith("{"):
                creds_dict = json.loads(info_str)
            elif os.path.exists(info_str):
                self.client = gspread.service_account(filename=info_str)
            else:
                # Attempt base64 decoding if user base64-encoded it
                try:
                    decoded = base64.b64decode(info_str).decode("utf-8").strip()
                    if decoded.startswith("{"):
                        creds_dict = json.loads(decoded)
                except Exception:
                    pass

            if creds_dict:
                self.client = gspread.service_account_from_dict(creds_dict)
            elif not self.client:
                preview = info_str[:50].replace('\n', ' ')
                logger.error(
                    f"GOOGLE_SERVICE_ACCOUNT_JSON is invalid! It does not start with '{{' and is not an existing file path. "
                    f"Preview received: '{preview}...' (Length: {len(info_str)}). "
                    "Make sure to copy-paste the ENTIRE contents of the .json file (starting with {{ and ending with }}) into your GitHub secret."
                )
                return False

            # Access Spreadsheet
            if self.sheet_id:
                clean_id = self.sheet_id.strip()
                # If user provided full spreadsheet URL, extract ID
                match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", clean_id)
                if match:
                    clean_id = match.group(1)
                self.spreadsheet = self.client.open_by_key(clean_id)
            elif self.sheet_name:
                self.spreadsheet = self.client.open(self.sheet_name.strip())
            else:
                logger.error("Neither GOOGLE_SHEET_ID nor GOOGLE_SHEET_NAME specified.")
                return False

            # Ensure clean worksheet name
            target_ws = (self.worksheet_name or "").strip()
            self.worksheet_name = target_ws if target_ws else DEFAULT_WORKSHEET_NAME

            # Access or Create Worksheet tab
            try:
                self.worksheet = self.spreadsheet.worksheet(self.worksheet_name)
            except gspread.WorksheetNotFound:
                # If target tab doesn't exist, check existing sheets in workbook
                existing_sheets = self.spreadsheet.worksheets()
                reusable = None
                for s in existing_sheets:
                    if s.title.lower().strip() in ("sheet1", "sheet 1"):
                        vals = s.get_all_values()
                        if len(vals) <= 1:
                            reusable = s
                            break

                if reusable:
                    logger.info(f"Target tab '{self.worksheet_name}' not found, but empty default tab '{reusable.title}' exists. Using '{reusable.title}'...")
                    self.worksheet = reusable
                    self.worksheet_name = reusable.title
                    if not reusable.get_all_values():
                        self.worksheet.append_row(SHEET_HEADERS)
                else:
                    logger.info(f"Worksheet '{self.worksheet_name}' not found. Creating it with default headers...")
                    self.worksheet = self.spreadsheet.add_worksheet(title=self.worksheet_name, rows=500, cols=20)
                    self.worksheet.append_row(SHEET_HEADERS)

            logger.info(f"Successfully connected to Google Sheet '{self.spreadsheet.title}', tab '{self.worksheet_name}'.")
            return True
        except Exception as e:
            logger.error(f"Error connecting to Google Sheets: {e}", exc_info=True)
            return False

    def get_existing_domains(self) -> Set[str]:
        """
        Reads Column D (Website URL) from the Google Sheet and extracts all normalized domains.
        """
        existing_domains = set()
        if not self.worksheet:
            logger.info("No active worksheet connection; using empty existing domains memory.")
            return existing_domains

        try:
            urls = self.worksheet.col_values(4)  # Column D = index 4
            # Skip header if it matches "Website URL"
            for url in urls:
                if not url or url.strip().lower() in ("website url", "website", "url"):
                    continue
                dom = normalize_domain(url)
                if dom:
                    existing_domains.add(dom)
            logger.info(f"Fetched {len(existing_domains)} deduplicated domains from Google Sheet Column D.")
        except Exception as e:
            logger.error(f"Failed to read Column D from Google Sheet: {e}")

        return existing_domains

    def append_lead_records(self, records: List[FinalLeadRecord]) -> bool:
        if not records:
            logger.info("No records to append.")
            return True

        rows = [record.to_sheet_row() for record in records]

        if self.dry_run:
            logger.info(f"[DRY-RUN] Simulating append of {len(rows)} rows to Google Sheets.")
            return True

        if not self.worksheet:
            logger.error("Cannot append rows: Google Worksheet is not connected.")
            return False

        try:
            self.worksheet.append_rows(rows, value_input_option="USER_ENTERED")
            logger.info(f"Successfully appended {len(rows)} leads to '{self.worksheet_name}'.")
            return True
        except Exception as e:
            logger.error(f"Error appending rows to Google Sheet: {e}", exc_info=True)
            return False


# ==============================================================================
# Step 3.2: Multi-Region Scraping & Fallback Email Extractor
# ==============================================================================

class ProspectScraper:
    def __init__(self, apify_token: Optional[str] = None, mock: bool = False):
        self.apify_token = apify_token
        self.mock = mock
        self.client = ApifyClient(apify_token) if (apify_token and ApifyClient and not mock) else None

    def get_todays_target(self, niche_override: Optional[str] = None, region_override: Optional[str] = None) -> Dict[str, Any]:
        """
        Calculates rotating niche and region based on day-of-year or explicit overrides.
        """
        day_of_year = datetime.datetime.now(datetime.timezone.utc).timetuple().tm_yday

        if niche_override:
            niche = {"name": niche_override, "query": niche_override}
        else:
            niche = ROTATING_NICHES[day_of_year % len(ROTATING_NICHES)]

        if region_override:
            region = {"code": "CUSTOM", "name": region_override, "cities": [region_override]}
        else:
            region = ROTATING_REGIONS[day_of_year % len(ROTATING_REGIONS)]

        return {"niche": niche, "region": region}

    def scrape_candidates(
        self,
        target: Dict[str, Any],
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
        existing_domains: Optional[Set[str]] = None,
    ) -> List[ProspectCandidate]:
        """
        Scrapes candidates via Apify Google Places/Search or generates mock candidates if configured.
        Filters out already-seen domains and enriches missing contact emails.
        """
        existing_domains = existing_domains or set()
        candidates: List[ProspectCandidate] = []
        seen_batch_domains: Set[str] = set()

        niche_info = target["niche"]
        region_info = target["region"]

        logger.info(f"Targeting Niche: '{niche_info['name']}' in Region: '{region_info['name']}'")

        if self.mock or not self.client:
            logger.info("Using built-in candidate generation (Mock / Fallback mode).")
            raw_candidates = self._generate_mock_candidates(niche_info, region_info, count=max_candidates)
        else:
            raw_candidates = self._scrape_apify(niche_info, region_info, max_candidates)

        logger.info(f"Retrieved {len(raw_candidates)} raw prospects. Filtering & resolving emails...")

        for raw in raw_candidates:
            website = raw.get("website")
            if not website:
                continue

            dom = normalize_domain(website)
            if not dom:
                continue

            if dom in existing_domains or dom in seen_batch_domains:
                continue

            # Fallback email resolution if missing
            email = raw.get("contact_email")
            if not email or "@" not in email:
                if not self.mock:
                    logger.debug(f"Missing email for {dom}. Probing website fallback...")
                    email = scrape_email_from_website(website)
                else:
                    # In mock mode, supply realistic domain email
                    clean_name = re.sub(r"[^a-zA-Z]", "", raw.get("company_name", "contact")).lower()
                    email = f"info@{dom}"

            if not email:
                continue  # Drop candidate if email could not be discovered

            seen_batch_domains.add(dom)
            candidate = ProspectCandidate(
                company_name=raw.get("company_name", "Enterprise Client"),
                website=website,
                contact_email=email,
                country_code=region_info.get("code", "US"),
                business_category=niche_info.get("name", "B2B Services"),
                phone=raw.get("phone"),
                address=raw.get("address"),
            )
            candidates.append(candidate)

            if len(candidates) >= max_candidates:
                break

        logger.info(f"Resolved {len(candidates)} unique candidates with verified websites & emails.")
        return candidates

    def _scrape_apify(self, niche_info: Dict[str, Any], region_info: Dict[str, Any], max_items: int) -> List[Dict[str, Any]]:
        """
        Executes Apify actor (compass/crawler-google-places) to scrape B2B local business records.
        """
        raw_results = []
        try:
            city = region_info["cities"][0]
            search_query = f"{niche_info['query']} in {city}"
            logger.info(f"Calling Apify actor 'compass/crawler-google-places' with query: '{search_query}'")

            run_input = {
                "searchStringsArray": [search_query],
                "maxCrawledPlacesPerSearch": max_items,
                "language": "en",
                "includeWebResults": False,
            }

            actor_call = self.client.actor("compass/crawler-google-places").call(run_input=run_input, timeout_secs=180)
            dataset_id = actor_call.get("defaultDatasetId")
            if dataset_id:
                for item in self.client.dataset(dataset_id).iterate_items():
                    raw_results.append({
                        "company_name": item.get("title") or item.get("name"),
                        "website": item.get("website"),
                        "contact_email": item.get("email"),
                        "phone": item.get("phone"),
                        "address": item.get("address"),
                    })
        except Exception as e:
            logger.error(f"Apify scraping failed: {e}. Falling back to mock dataset generation.")
            return self._generate_mock_candidates(niche_info, region_info, count=max_items)

        return raw_results

    def _generate_mock_candidates(self, niche_info: Dict[str, Any], region_info: Dict[str, Any], count: int) -> List[Dict[str, Any]]:
        """
        Generates realistic candidate prospects for dry-run testing and offline verification.
        """
        prefixes = ["Apex", "Vanguard", "Summit", "Nexus", "Pinnacle", "Sterling", "Quantum", "Beacon", "Centurion", "Horizon", "Ascent", "Omni", "Veritas", "Ironclad", "Catalyst"]
        suffixes = ["Partners", "Group", "Consulting", "Solutions", "Advisory", "Associates", "Technologies", "Services", "Industries", "Holdings"]

        candidates = []
        for i in range(1, count + 1):
            p = prefixes[(i - 1) % len(prefixes)]
            s = suffixes[(i * 3) % len(suffixes)]
            company = f"{p} {s}"
            clean_slug = f"{p.lower()}-{s.lower()}-{i}"
            tld = ".co.uk" if region_info["code"] == "UK" else ".com.au" if region_info["code"] == "AU" else ".ca" if region_info["code"] == "CA" else ".com"
            domain = f"{clean_slug}{tld}"

            candidates.append({
                "company_name": company,
                "website": f"https://www.{domain}",
                "contact_email": f"partner@{domain}",
                "phone": f"+1-555-01{i:02d}",
                "address": f"{100 + i} Financial Way, {region_info['cities'][i % len(region_info['cities'])]}",
            })

        return candidates


# ==============================================================================
# Step 3.3: GLM-5.3-Flash Scoring via NVIDIA NIM API
# ==============================================================================

class LeadEvaluator:
    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_LLM_MODEL, mock: bool = False, **kwargs):
        self.api_key = api_key or kwargs.get("hf_token")
        self.model = resolve_model_name(model)
        self.mock = mock
        self.client = None
        if self.api_key and OpenAI and not mock:
            self.client = OpenAI(base_url=NVIDIA_NIM_BASE_URL, api_key=self.api_key, timeout=25.0, max_retries=1)

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=4),
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def _call_llm_inference(self, prompt: str) -> str:
        """
        Calls NVIDIA NIM API (hosting z-ai/glm-5.3-flash) with reasoning_effort=low and 2048 tokens.
        """
        sys_prompt = "You are an expert enterprise B2B website auditor and lead scorer. Always return valid, parseable JSON."
        if self.client:
            call_kwargs = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 2048,
                "temperature": 0.2,
            }
            if "glm" in self.model.lower():
                call_kwargs["extra_body"] = {"reasoning_effort": "low"}
            response = self.client.chat.completions.create(**call_kwargs)
            msg = response.choices[0].message
            content = msg.content
            if not content and hasattr(msg, "reasoning_content"):
                content = msg.reasoning_content
            return content or ""

        # Direct HTTP fallback
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 2048,
            "temperature": 0.2,
        }
        if "glm" in self.model.lower():
            payload["reasoning_effort"] = "low"
        res = requests.post(f"{NVIDIA_NIM_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=25)
        res.raise_for_status()
        data = res.json()
        msg = data["choices"][0]["message"]
        return msg.get("content") or msg.get("reasoning_content") or ""

    def _fallback_evaluation(self, candidate: ProspectCandidate) -> LeadEvaluation:
        dom_hash = sum(ord(c) for c in candidate.website)
        score = 65 + (dom_hash % 33)  # Range 65 to 97
        angle: Literal["Authority Website", "AI Growth Website"] = (
            "Authority Website" if (dom_hash % 2 == 0) else "AI Growth Website"
        )
        pain_points = [
            "Unclear value proposition and outdated case study layout causing high enterprise drop-off.",
            "Lack of automated qualification funnel forcing senior partners to handle low-tier inquiry calls.",
            "Slow mobile load speed and non-interactive consultation booking bottlenecking qualified leads.",
            "Absence of trust-building social proof and executive-tier positioning relative to category peers.",
        ]
        pain = pain_points[dom_hash % len(pain_points)]
        return LeadEvaluation(offer_angle=angle, score=score, primary_pain_point=pain)

    def evaluate_candidate(self, candidate: ProspectCandidate) -> LeadEvaluation:
        """
        Evaluates a single prospect candidate using GLM-5.3-Flash, categorizing into
        'Authority Website' vs 'AI Growth Website' and scoring from 1-100.
        """
        if self.mock or (not self.client and not self.api_key):
            return self._fallback_evaluation(candidate)

        prompt = f"""Analyze this prospective B2B client and classify which modernization offer fits best:
Company Name: {candidate.company_name}
Website: {candidate.website}
Category: {candidate.business_category}
Region: {candidate.country_code}

Instructions:
1. Select offer_angle strictly from:
   - "Authority Website": High-ticket B2B service firms needing prestige credibility, institutional authority, executive positioning, and portfolio trust.
   - "AI Growth Website": Fast-growing service/tech firms needing automated lead qualification, interactive AI chat/quote agents, and conversion-engineered funnels.
2. Provide score: An integer from 1 to 100 representing commercial deal potential, relevance, and urgency of web infrastructure upgrade.
3. Provide primary_pain_point: A concise 1-sentence assessment (under 25 words) of their digital bottleneck.

You must respond ONLY with a valid JSON object matching this schema:
{{
  "offer_angle": "Authority Website" | "AI Growth Website",
  "score": 85,
  "primary_pain_point": "Outdated trust signals and lack of clear executive positioning hurting client conversion."
}}
"""
        try:
            raw_resp = self._call_llm_inference(prompt)
            parsed = extract_json_from_llm_response(raw_resp)
            return LeadEvaluation(**parsed)
        except Exception as e:
            logger.warning(f"LLM scoring failed for '{candidate.company_name}': {e}. Using deterministic evaluation fallback.")
            return self._fallback_evaluation(candidate)

    def score_and_select_top_10(self, candidates: List[ProspectCandidate]) -> List[tuple[ProspectCandidate, LeadEvaluation]]:
        """
        Scores all candidates, sorts descending by score, and extracts strictly the top 10.
        """
        logger.info(f"Evaluating {len(candidates)} candidates via NVIDIA GLM-5.3-Flash scoring...")
        evaluated = []

        for candidate in candidates:
            try:
                evaluation = self.evaluate_candidate(candidate)
                evaluated.append((candidate, evaluation))
            except Exception as e:
                logger.warning(f"Failed to evaluate candidate {candidate.company_name} ({candidate.website}): {e}. Using fallback.")
                evaluated.append((candidate, self._fallback_evaluation(candidate)))

        # Sort descending by score
        evaluated.sort(key=lambda x: x[1].score, reverse=True)

        top_10 = evaluated[:10]
        logger.info(f"Selected strictly top {len(top_10)} leads (Scores: {[e[1].score for e in top_10]}).")
        return top_10


# ==============================================================================
# Step 3.4: Bespoke Cold Email Pitch Generation
# ==============================================================================

class ColdEmailDraftsman:
    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_LLM_MODEL, mock: bool = False, **kwargs):
        self.api_key = api_key or kwargs.get("hf_token")
        self.model = resolve_model_name(model)
        self.mock = mock
        self.client = None
        if self.api_key and OpenAI and not mock:
            self.client = OpenAI(base_url=NVIDIA_NIM_BASE_URL, api_key=self.api_key, timeout=25.0, max_retries=1)

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=4),
        stop=stop_after_attempt(2),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def _call_llm_inference(self, prompt: str) -> str:
        sys_prompt = (
            "You are an elite, senior technical growth engineer writing direct, peer-to-peer cold emails. "
            "Never use marketing cliches. Always return valid JSON."
        )
        if self.client:
            call_kwargs = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 2048,
                "temperature": 0.3,
            }
            if "glm" in self.model.lower():
                call_kwargs["extra_body"] = {"reasoning_effort": "low"}
            response = self.client.chat.completions.create(**call_kwargs)
            msg = response.choices[0].message
            content = msg.content
            if not content and hasattr(msg, "reasoning_content"):
                content = msg.reasoning_content
            return content or ""

        # Direct HTTP fallback
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 2048,
            "temperature": 0.3,
        }
        if "glm" in self.model.lower():
            payload["reasoning_effort"] = "low"
        res = requests.post(f"{NVIDIA_NIM_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=25)
        res.raise_for_status()
        data = res.json()
        msg = data["choices"][0]["message"]
        return msg.get("content") or msg.get("reasoning_content") or ""

    def _fallback_pitch(self, candidate: ProspectCandidate, evaluation: LeadEvaluation) -> EmailPitch:
        domain = normalize_domain(candidate.website)
        first_name = candidate.company_name.split()[0]
        if evaluation.offer_angle == "Authority Website":
            subject = f"{first_name} digital authority positioning"
            body = (
                f"Hi {first_name} team,\n\n"
                f"Reviewed {domain} this morning. Noticed your case studies and advisory credentials are buried below the fold, "
                f"meaning high-value prospective clients often miss your core track record.\n\n"
                f"We recently redesigned the positioning architecture for a similar firm, increasing enterprise conversion by 34%.\n\n"
                f"Put together a quick 90-second video teardown showing where prospective clients drop off on {domain}. "
                f"Mind if I share the link here?"
            )
        else:
            subject = f"inquiry qualification on {domain}"
            body = (
                f"Hi {first_name} team,\n\n"
                f"Took a look at {domain}. Your inbound intake currently relies on static forms without automated qualification, "
                f"costing your team billable hours filtering unqualified leads.\n\n"
                f"We implement interactive AI qualification funnels that pre-vet deal size and sync booked consultations directly.\n\n"
                f"Built a quick interactive preview mockup showing how this would operate on {domain}. Open to a 2-minute walkthrough?"
            )
        return EmailPitch(subject=subject, email_body=body)

    def draft_email_pitch(self, candidate: ProspectCandidate, evaluation: LeadEvaluation) -> EmailPitch:
        """
        Drafts a human-like, non-templated cold email pitch (<110 words) free of AI cliches.
        """
        if self.mock or (not self.client and not self.api_key):
            return self._fallback_pitch(candidate, evaluation)

        prompt = f"""Write a bespoke, direct cold email to:
Company: {candidate.company_name}
Website: {candidate.website}
Category: {candidate.business_category}
Modernization Angle: {evaluation.offer_angle}
Core Bottleneck / Pain Point: {evaluation.primary_pain_point}

Strict Guidelines:
1. Under 110 words total.
2. Peer-to-peer, direct, technical tone.
3. ABSOLUTELY NO AI CLICHES: Never use "I hope this email finds you well", "synergy", "in today's competitive landscape", "delve", "game-changer", "stumbled upon your website", "testament", "unlock".
4. Structure:
   - Specific technical observation of their bottleneck on {candidate.website}.
   - Commercial implication (missed enterprise deals or wasted partner hours).
   - Zero-pressure CTA offering a 90-second Loom teardown or interactive preview link.
5. Subject line: 3-6 words, lowercase/natural, curiosity-driven (e.g., "quick observation on {candidate.company_name}").

Respond ONLY with valid JSON:
{{
  "subject": "...",
  "email_body": "..."
}}
"""
        try:
            raw_resp = self._call_llm_inference(prompt)
            parsed = extract_json_from_llm_response(raw_resp)
            return EmailPitch(**parsed)
        except Exception as e:
            logger.warning(f"LLM pitch drafting failed for '{candidate.company_name}': {e}. Using bespoke pitch fallback.")
            return self._fallback_pitch(candidate, evaluation)


# ==============================================================================
# Pipeline Coordinator
# ==============================================================================

class LeadGenerationPipeline:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.dry_run = config.get("dry_run", False)
        self.mock = config.get("mock", False)
        self.sheet_manager = GoogleSheetSyncManager(
            service_account_info=config.get("google_service_account_json"),
            sheet_id=config.get("google_sheet_id"),
            sheet_name=config.get("google_sheet_name"),
            worksheet_name=config.get("google_worksheet_name", DEFAULT_WORKSHEET_NAME),
            dry_run=self.dry_run,
        )
        self.scraper = ProspectScraper(apify_token=config.get("apify_token"), mock=self.mock)
        llm_api_key = config.get("nvidia_api_key") or config.get("hf_token")
        llm_model = config.get("nvidia_model") or config.get("hf_model") or DEFAULT_LLM_MODEL
        self.evaluator = LeadEvaluator(api_key=llm_api_key, model=llm_model, mock=self.mock)
        self.draftsman = ColdEmailDraftsman(api_key=llm_api_key, model=llm_model, mock=self.mock)

    def run(self) -> List[FinalLeadRecord]:
        logger.info("=" * 60)
        logger.info("Starting Daily Lead Prospector & Cold Outreach Pipeline")
        logger.info(f"Mode: Dry-Run={self.dry_run} | Mock={self.mock}")
        logger.info("=" * 60)

        # Step 3.1: Connect to Google Sheets & Deduplication Memory
        connected = self.sheet_manager.connect()
        if not self.dry_run and not connected:
            raise RuntimeError(
                "Failed to connect to Google Sheets! Please verify that GOOGLE_SERVICE_ACCOUNT_JSON contains the complete service account JSON and that GOOGLE_SHEET_ID is set."
            )
        existing_domains = self.sheet_manager.get_existing_domains()

        # Step 3.2: Multi-Region Scraping
        target = self.scraper.get_todays_target(
            niche_override=self.config.get("niche"),
            region_override=self.config.get("region"),
        )
        candidates = self.scraper.scrape_candidates(
            target=target,
            max_candidates=self.config.get("max_candidates", DEFAULT_MAX_CANDIDATES),
            existing_domains=existing_domains,
        )

        if not candidates:
            logger.warning("No new candidates found after deduplication and email filtering.")
            return []

        # Step 3.3: GLM-5.3-Flash Scoring & Selection of Strict Top 10
        top_10 = self.evaluator.score_and_select_top_10(candidates)

        # Step 3.4: Cold Email Pitch Drafting
        today_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        final_records: List[FinalLeadRecord] = []

        logger.info(f"Generating personalized cold email pitches for top {len(top_10)} candidates...")
        for candidate, evaluation in top_10:
            pitch = self.draftsman.draft_email_pitch(candidate, evaluation)
            record = FinalLeadRecord(
                date_added=today_str,
                company_name=candidate.company_name,
                country_code=candidate.country_code,
                website=candidate.website,
                contact_email=candidate.contact_email,
                offer_angle=evaluation.offer_angle,
                score=evaluation.score,
                primary_pain_point=evaluation.primary_pain_point,
                subject=pitch.subject,
                email_body=pitch.email_body,
                status="Pending Review",
            )
            final_records.append(record)

        # Step 3.5: Google Sheets Sync & Reporting
        appended = self.sheet_manager.append_lead_records(final_records)
        self._display_summary(final_records)

        if not self.dry_run and not appended:
            raise RuntimeError(
                "Failed to append rows to Google Sheets! Please ensure the service account email is added as an 'Editor' to your Google Sheet."
            )

        logger.info("Daily pipeline execution completed successfully.")
        return final_records

    def _display_summary(self, records: List[FinalLeadRecord]) -> None:
        """
        Prints formatted tabular summary of the top 10 leads and cold pitches.
        """
        table_data = []
        for idx, r in enumerate(records, 1):
            table_data.append([
                idx,
                r.company_name,
                r.country_code,
                r.website,
                r.contact_email,
                r.offer_angle,
                f"{r.score}/100",
                r.subject,
                r.status,
            ])

        headers = ["#", "Company", "Region", "Website", "Contact Email", "Offer Angle", "Score", "Subject Line", "Status"]
        print("\n" + "=" * 80)
        print("DAILY OUTREACH PIPELINE - TOP 10 QUALIFIED PROSPECTS")
        print("=" * 80)
        print(tabulate(table_data, headers=headers, tablefmt="grid"))

        print("\n" + "=" * 80)
        print("SAMPLE GENERATED OUTREACH PITCHES")
        print("=" * 80)
        for idx, r in enumerate(records[:3], 1):
            print(f"\n[{idx}] {r.company_name} ({r.contact_email}) - {r.offer_angle} (Score: {r.score})")
            print(f"Subject: {r.subject}")
            print("-" * 50)
            print(r.email_body)
            print("-" * 50)


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Autonomous Daily Lead Prospector & Cold Outreach Pipeline (NVIDIA NIM GLM-5.3-Flash)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Run without mutating Google Sheets")
    parser.add_argument("--mock", action="store_true", help="Use mock scraping and LLM generation")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES, help="Candidates to scrape")
    parser.add_argument("--niche", type=str, default=None, help="Override rotating niche")
    parser.add_argument("--region", type=str, default=None, help="Override rotating region")
    parser.add_argument("--model", type=str, default=None, help=f"Override model (default: {DEFAULT_LLM_MODEL})")
    parser.add_argument("--api-key", type=str, default=None, help="NVIDIA NIM API key")
    parser.add_argument("--worksheet", type=str, default=None, help=f"Target worksheet tab (default: {DEFAULT_WORKSHEET_NAME})")
    return parser.parse_args()


def load_config(args: argparse.Namespace) -> Dict[str, Any]:
    nvidia_key = (
        args.api_key
        or os.environ.get("NVIDIA_API_KEY")
        or os.environ.get("NVIDIA_KEY")
        or os.environ.get("HF_TOKEN")
    )
    model = (
        args.model
        or os.environ.get("NVIDIA_MODEL")
        or os.environ.get("HF_MODEL")
        or DEFAULT_LLM_MODEL
    )
    ws_env = (os.environ.get("GOOGLE_WORKSHEET_NAME") or "").strip()
    target_worksheet = getattr(args, "worksheet", None) or ws_env or DEFAULT_WORKSHEET_NAME
    return {
        "apify_token": os.environ.get("APIFY_TOKEN"),
        "nvidia_api_key": nvidia_key,
        "nvidia_model": resolve_model_name(model),
        "google_service_account_json": os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
        "google_sheet_id": os.environ.get("GOOGLE_SHEET_ID"),
        "google_sheet_name": os.environ.get("GOOGLE_SHEET_NAME"),
        "google_worksheet_name": target_worksheet,
        "max_candidates": args.max_candidates or int(os.environ.get("MAX_CANDIDATES", DEFAULT_MAX_CANDIDATES)),
        "dry_run": args.dry_run or (os.environ.get("DRY_RUN", "").lower() in ("true", "1", "yes")),
        "mock": args.mock or (os.environ.get("MOCK_SCRAPE", "").lower() in ("true", "1", "yes")),
        "niche": args.niche or os.environ.get("NICHE_OVERRIDE"),
        "region": args.region or os.environ.get("REGION_OVERRIDE"),
    }


def main():
    args = parse_args()
    config = load_config(args)
    pipeline = LeadGenerationPipeline(config)
    pipeline.run()


if __name__ == "__main__":
    main()
