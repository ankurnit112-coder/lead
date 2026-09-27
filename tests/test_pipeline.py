import json
import pytest
from pipeline import (
    FinalLeadRecord,
    GoogleSheetSyncManager,
    LeadEvaluation,
    LeadEvaluator,
    LeadGenerationPipeline,
    ProspectCandidate,
    ProspectScraper,
    extract_emails_from_text,
    extract_json_from_llm_response,
    normalize_domain,
)


class TestDomainNormalization:
    @pytest.mark.parametrize(
        "raw_url,expected",
        [
            ("https://www.ApexLawFirm.co.uk/about?ref=1/", "apexlawfirm.co.uk"),
            ("http://vanguard-tech.com/contact-us", "vanguard-tech.com"),
            ("www.summitconsulting.org:8080/services", "summitconsulting.org"),
            ("https://subdomain.domain.com/path", "subdomain.domain.com"),
            ("http://pinnacle.ca", "pinnacle.ca"),
            ("HTTPS://WWW.EXAMPLE.COM/", "example.com"),
            ("", ""),
            (None, ""),
            ("invalid-url", "invalid-url"),
        ],
    )
    def test_normalize_domain(self, raw_url, expected):
        assert normalize_domain(raw_url) == expected


class TestJSONExtraction:
    def test_pure_json(self):
        sample = '{"offer_angle": "Authority Website", "score": 88, "primary_pain_point": "Outdated design"}'
        parsed = extract_json_from_llm_response(sample)
        assert parsed["offer_angle"] == "Authority Website"
        assert parsed["score"] == 88

    def test_markdown_json_fence(self):
        sample = """Here is your assessment:
```json
{
  "offer_angle": "AI Growth Website",
  "score": 92,
  "primary_pain_point": "Static contact form lacks qualification funnel"
}
```
Hope this helps!"""
        parsed = extract_json_from_llm_response(sample)
        assert parsed["offer_angle"] == "AI Growth Website"
        assert parsed["score"] == 92

    def test_markdown_generic_fence(self):
        sample = """```
{
  "offer_angle": "Authority Website",
  "score": 75,
  "primary_pain_point": "Buried case studies"
}
```"""
        parsed = extract_json_from_llm_response(sample)
        assert parsed["offer_angle"] == "Authority Website"
        assert parsed["score"] == 75

    def test_conversational_wrapped_json(self):
        sample = """Sure! Based on my evaluation of the enterprise firm:
{"offer_angle": "Authority Website", "score": 95, "primary_pain_point": "Zero executive proof"}
Let me know if you need more details."""
        parsed = extract_json_from_llm_response(sample)
        assert parsed["score"] == 95

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            extract_json_from_llm_response("Not a JSON response at all.")


class TestEmailExtraction:
    def test_extract_emails_from_text(self):
        sample_text = """
        Contact us at hello@apexlaw.com or inquiries@apexlaw.com.
        Avoid icon@2x.png and test@example.com or user@wixpress.com.
        Support: info@apexlaw.co.uk.
        """
        emails = extract_emails_from_text(sample_text)
        assert "hello@apexlaw.com" in emails
        assert "inquiries@apexlaw.com" in emails
        assert "info@apexlaw.co.uk" in emails
        assert "test@example.com" not in emails
        assert "user@wixpress.com" not in emails
        assert "icon@2x.png" not in emails


class TestScraperCandidateFiltering:
    def test_deduplication_against_existing_and_batch(self):
        scraper = ProspectScraper(mock=True)
        existing = {"summit-advisory-1.com", "apex-partners-1.com"}
        target = {
            "niche": {"name": "Commercial Roofing", "query": "commercial roofing"},
            "region": {"code": "US", "name": "United States", "cities": ["Austin, TX"]},
        }
        candidates = scraper.scrape_candidates(target, max_candidates=20, existing_domains=existing)

        for c in candidates:
            dom = normalize_domain(c.website)
            assert dom not in existing
            assert c.contact_email is not None
            assert "@" in c.contact_email


class TestEndToEndPipeline:
    def test_mock_dry_run_pipeline(self):
        config = {
            "dry_run": True,
            "mock": True,
            "max_candidates": 30,
        }
        pipeline = LeadGenerationPipeline(config)
        records = pipeline.run()

        # Strict top 10 leads check
        assert len(records) == 10

        # Check sorting order: scores must be descending
        scores = [r.score for r in records]
        assert scores == sorted(scores, reverse=True)

        for record in records:
            # Check fields
            assert record.status == "Pending Review"
            assert record.offer_angle in ("Authority Website", "AI Growth Website")
            assert 1 <= record.score <= 100
            assert record.contact_email and "@" in record.contact_email
            assert record.website.startswith("http")

            # Word count check (<110 words)
            word_count = len(record.email_body.split())
            assert word_count <= 110

            # 11 columns in row output
            row = record.to_sheet_row()
            assert len(row) == 11
            assert row[10] == "Pending Review"
