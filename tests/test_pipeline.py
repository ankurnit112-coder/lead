import json
from unittest.mock import MagicMock
import pytest
from pipeline import (
    BANNED_BUZZWORDS,
    ColdEmailDraftsman,
    EmailPitch,
    FinalLeadRecord,
    GoogleSheetSyncManager,
    LeadEvaluation,
    LeadEvaluator,
    LeadGenerationPipeline,
    ProspectCandidate,
    ProspectScraper,
    extract_emails_from_text,
    extract_json_from_llm_response,
    is_valid_contact_email,
    normalize_domain,
    rank_email_address,
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


class TestEmailValidation:
    @pytest.mark.parametrize(
        "placeholder_email",
        [
            "you@company.com",
            "yourname@company.com",
            "user@example.com",
            "name@yourcompany.com",
            "placeholder@test.com",
            "test@sample.com",
            "admin@domain.com",
            "webmaster@website.com",
            "postmaster@sitename.com",
            "firstname@mycompany.com",
            "lastname@placeholder.com",
            "demo@example.com",
            "info@company.com",
            "contact@wixpress.com",
        ],
    )
    def test_placeholder_emails_rejected(self, placeholder_email):
        assert not is_valid_contact_email(placeholder_email)

    @pytest.mark.parametrize(
        "valid_email,domain",
        [
            ("partner@berkeleylaw.com", "berkeleylaw.com"),
            ("contact@pearsonstrategy.com", "pearsonstrategy.com"),
            ("inquiries@apexroofing.com", "apexroofing.com"),
            ("john.smith@vanguardlegal.co.uk", "vanguardlegal.co.uk"),
            ("sarah_connor@techadvisory.ca", "techadvisory.ca"),
            ("ceo@innovationsystems.com", "innovationsystems.com"),
            ("info@summitmep.com.au", "summitmep.com.au"),
        ],
    )
    def test_valid_emails_accepted(self, valid_email, domain):
        assert is_valid_contact_email(valid_email, company_domain=domain)

    def test_invalid_formats_and_artifacts(self):
        assert not is_valid_contact_email("")
        assert not is_valid_contact_email(None)
        assert not is_valid_contact_email("plainaddress")
        assert not is_valid_contact_email("@missingusername.com")
        assert not is_valid_contact_email("missingdomain@")
        assert not is_valid_contact_email("icon@2x.png")
        assert not is_valid_contact_email("logo@brand.svg")
        assert not is_valid_contact_email("user@sentry.io")


class TestMailboxTierRanking:
    def test_rank_hierarchy(self):
        partner_rank = rank_email_address("partner@berkeleylaw.com")
        inquiry_rank = rank_email_address("inquiries@berkeleylaw.com")
        support_rank = rank_email_address("support@berkeleylaw.com")

        assert partner_rank == 100
        assert inquiry_rank == 70
        assert support_rank == 30
        assert partner_rank > inquiry_rank > support_rank

    @pytest.mark.parametrize(
        "email,expected_tier",
        [
            ("founder@apex.com", 100),
            ("principal@lawfirm.com", 100),
            ("managingpartner@consulting.com", 100),
            ("attorney@lawgroup.com", 100),
            ("counsel@ipfirm.com", 100),
            ("ceo@enterprise.com", 100),
            ("growth@startup.com", 100),
            ("alex.taylor@company.com", 100),
            ("jane_doe@firm.co.uk", 100),
            ("contact@company.com", 70),
            ("hello@agency.com", 70),
            ("office@advisory.com", 70),
            ("info@contractor.com", 70),
            ("intake@legal.com", 70),
            ("support@techdesk.com", 30),
            ("help@systems.com", 30),
            ("billing@services.com", 30),
            ("jobs@corporation.com", 30),
            ("service@repairs.com", 30),
        ],
    )
    def test_individual_mailbox_tiers(self, email, expected_tier):
        assert rank_email_address(email) == expected_tier


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


class TestAntiBuzzwordCopyAndDynamicSubjects:
    def test_no_banned_buzzwords_in_fallback_drafts(self):
        draftsman = ColdEmailDraftsman(mock=True)
        candidates = [
            ProspectCandidate(
                company_name=f"Firm {i}",
                website=f"https://www.firm-{i}.com",
                contact_email=f"partner@firm-{i}.com",
                country_code="US",
                business_category="Legal Advisory",
            )
            for i in range(1, 11)
        ]

        for i, candidate in enumerate(candidates):
            angle = "Authority Website" if (i % 2 == 0) else "AI Growth Website"
            evaluation = LeadEvaluation(
                offer_angle=angle,
                score=85,
                primary_pain_point="Bottlenecks in digital consultation bookings.",
            )
            pitch = draftsman.draft_email_pitch(candidate, evaluation)

            full_pitch_text = f"{pitch.subject} {pitch.email_body}".lower()
            for buzzword in BANNED_BUZZWORDS:
                assert buzzword.lower() not in full_pitch_text, (
                    f"Banned buzzword '{buzzword}' found in pitch for {candidate.company_name}"
                )

            # Check word count constraint
            words = pitch.email_body.split()
            assert len(words) <= 110, f"Email body exceeded 110 words: {len(words)} words"

    def test_subject_line_variety(self):
        draftsman = ColdEmailDraftsman(mock=True)
        candidates = [
            ProspectCandidate(
                company_name=name,
                website=f"https://www.{name.lower().replace(' ', '')}.com",
                contact_email=f"partner@{name.lower().replace(' ', '')}.com",
                country_code="US",
                business_category="Consulting",
            )
            for name in ["Apex Advisory", "Vanguard Partners", "Pinnacle Group", "Sterling Legal", "Horizon Growth"]
        ]

        subjects = []
        for i, candidate in enumerate(candidates):
            angle = "Authority Website" if (i % 2 == 0) else "AI Growth Website"
            eval_record = LeadEvaluation(offer_angle=angle, score=90, primary_pain_point="Outdated client intake flow")
            pitch = draftsman.draft_email_pitch(candidate, eval_record)
            subjects.append(pitch.subject)

        # Ensure dynamic diversification (not all identical)
        assert len(set(subjects)) > 1
        # Check that none use the old rigid repetitive pattern
        for s in subjects:
            assert "digital authority positioning" not in s.lower()


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
            assert is_valid_contact_email(c.contact_email, dom)


class TestSheetHygieneCleanup:
    def test_cleanup_invalid_records_flags_placeholders(self):
        manager = GoogleSheetSyncManager(dry_run=False)
        mock_worksheet = MagicMock()
        manager.worksheet = mock_worksheet

        # Header + 3 data rows: 1 valid, 2 placeholders/invalid
        mock_worksheet.get_all_values.return_value = [
            ["Date Added", "Company Name", "Country", "Website URL", "Contact Email", "Angle", "Score", "Pain", "Subject", "Pitch", "Status"],
            ["2026-09-28", "Legit Corp", "US", "https://legitcorp.com", "partner@legitcorp.com", "Authority Website", "90", "Pain", "Sub", "Body", "Pending Review"],
            ["2026-09-28", "Dummy Firm", "US", "https://dummyfirm.com", "you@company.com", "Authority Website", "80", "Pain", "Sub", "Body", "Pending Review"],
            ["2026-09-28", "Sample Ltd", "UK", "https://sampleltd.co.uk", "user@example.com", "AI Growth Website", "75", "Pain", "Sub", "Body", "Pending Review"],
        ]

        flagged = manager.cleanup_invalid_records()
        assert flagged == 2

        # Check update_cell calls: row 3 (you@company.com) and row 4 (user@example.com)
        mock_worksheet.update_cell.assert_any_call(3, 11, "Invalid Email - Flagged")
        mock_worksheet.update_cell.assert_any_call(4, 11, "Invalid Email - Flagged")


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
            assert is_valid_contact_email(record.contact_email, normalize_domain(record.website))
            assert record.website.startswith("http")

            # Word count check (<110 words)
            word_count = len(record.email_body.split())
            assert word_count <= 110

            # 11 columns in row output
            row = record.to_sheet_row()
            assert len(row) == 11
            assert row[10] == "Pending Review"
