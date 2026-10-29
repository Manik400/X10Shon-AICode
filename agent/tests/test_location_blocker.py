"""Postings whose own facts rule the candidate out by location (Wellfound's job facts, a site's
"not hiring in your location" notice) are skipped before anything is filled."""

from __future__ import annotations

from app.jobs.matching import location_blocker

INDIA = {"contact": {"country": "India"}, "legal": {"work_authorized_countries": ["India"]}}
NOWHERE: dict = {"contact": {}, "legal": {}}

WF_ONSITE_SF = "Kato Actively Hiring Save Apply Now Software Engineer San Francisco | 1 year of exp | Full Time Job Location San Francisco Remote Work Policy In office - WFH flexibility Visa Sponsorship Not Available Relocation Not Allowed Skills PostgreSQL About the job Company Overview"
WF_REMOTE_US = "Save Apply Now Backend Engineer Remote Job Location Remote Remote Work Policy Remote only Hires remotely in United States Visa Sponsorship Not Available Relocation Not Allowed Skills Go About the job"
WF_REMOTE_IN = "Save Apply Now Backend Engineer Remote Job Location Bengaluru Remote Work Policy Remote only Hires remotely in India, United States Visa Sponsorship Not Available Skills Go About the job"
WF_ONSITE_VISA = "Save Apply Now Engineer Berlin Job Location Berlin Remote Work Policy In office Visa Sponsorship Available Relocation Allowed Skills Python About the job"


def test_onsite_elsewhere_without_visa_or_relocation_is_blocked() -> None:
    why = location_blocker(WF_ONSITE_SF, INDIA)
    assert why and "San Francisco" in why and "no visa sponsorship" in why and "India" in why


def test_remote_job_hiring_only_in_other_countries_is_blocked() -> None:
    why = location_blocker(WF_REMOTE_US, INDIA)
    assert why and "hires remotely only in United States" in why


def test_remote_job_hiring_in_the_candidates_country_passes() -> None:
    assert location_blocker(WF_REMOTE_IN, INDIA) is None


def test_onsite_with_sponsorship_and_relocation_passes() -> None:
    assert location_blocker(WF_ONSITE_VISA, INDIA) is None


def test_no_known_candidate_location_means_no_verdict() -> None:
    assert location_blocker(WF_ONSITE_SF, NOWHERE) is None
    assert location_blocker(WF_REMOTE_US, NOWHERE) is None


def test_sites_own_not_hiring_in_your_location_notice() -> None:
    why = location_blocker("Apply Sorry, this company is not hiring in your location right now.", NOWHERE)
    assert why and "not hiring in your location" in why
    why = location_blocker("Candidates must be located in the United States to be considered.", INDIA)
    assert why and "United States" in why
    assert location_blocker("Candidates must be located in India or the United States.", INDIA) is None


def test_plain_job_description_is_not_blocked() -> None:
    assert location_blocker("We are a remote-first team across India and Europe. Apply now. Skills Python", INDIA) is None
