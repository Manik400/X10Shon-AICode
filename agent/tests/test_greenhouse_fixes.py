from __future__ import annotations

import pytest

from app.applications.field_mapping import build_fill_plan
from app.applications.form_filler import FormFiller
from app.applications.profile import CandidateProfile
from app.browser.snapshot import SnapshotNode
from app.browser.verifier import values_match
from app.jobs.matching import eligibility_blockers


def test_values_match_tolerates_site_formatting() -> None:
    assert values_match("text", "+91 9315605606", "+91 93156 05606")
    assert values_match("text", "+91 9315605606", "93156 05606")  # national number, separate country code
    assert not values_match("text", "+91 9315605606", "+91 93156 05607")
    assert values_match("select", "Gurugram", "Gurugram, Haryana, India")
    assert values_match("select", "Yes", "Yes")
    assert not values_match("select", "No", "None of the above")
    assert values_match("select", "Decline to self-identify", "I don't wish to answer")
    assert not values_match("select", "India", "")
    assert not values_match("text", "Manik", "Mani")


def test_input_combobox_is_planned_as_a_dropdown_pick(sample_profile) -> None:
    p = CandidateProfile.model_validate({**sample_profile, "contact": {**sample_profile["contact"], "country": "India"}})
    nodes = [SnapshotNode(i=6, role="combobox", name="Country*", tag="input", type="text", required=True, visible=True)]
    plan = build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"])
    f = plan.fields[0]
    assert f.field_type == "combobox" and f.value == "India"
    act = FormFiller.action_for(f)
    assert act is not None and act.type == "select" and act.args["value"] == "India"


def test_attach_button_in_cover_letter_section_is_not_given_the_resume(sample_profile) -> None:
    p = CandidateProfile.model_validate(sample_profile)
    resumes = [{"id": "r1", "label": "Default", "file_name": "cv.pdf"}]
    nodes = [
        SnapshotNode(i=1, role="file", name="Attach", tag="input", type="file", section="Resume/CV*", required=True, visible=True),
        SnapshotNode(i=2, role="file", name="Attach", tag="input", type="file", section="Cover Letter", visible=True),
    ]
    plan = build_fill_plan(nodes, p.facts(), [], None, resumes=resumes, default_resume_id="r1", submit_names=["Submit"])
    by = {f.node: f for f in plan.fields}
    assert by[1].status == "auto" and by[1].resume_id == "r1"
    assert by[2].status == "skip"


def test_require_visa_question_is_a_sponsorship_question(sample_profile) -> None:
    from app.applications.profile import countries_in, legal_category_for

    q = "Do you require a visa/work permit to work in the EU?*"
    assert legal_category_for(q) == "requires_sponsorship"
    assert legal_category_for("Are you legally authorized to work in the United States?") == "work_authorization"
    assert countries_in(q) == {"European Union"}
    assert "United States" in countries_in("Are you authorized to work in the U.S.?")
    p = CandidateProfile.model_validate({**sample_profile, "legal": {"work_authorized_countries": ["India"], "requires_sponsorship": "Yes"}})
    nodes = [SnapshotNode(i=0, role="combobox", name=q, tag="select", type="select", options=["Yes", "No"], required=True, visible=True)]
    f = build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields[0]
    assert (f.status, f.value) == ("auto", "Yes")


def test_salary_citizenship_languages_and_start_date(sample_profile) -> None:
    profile = {
        **sample_profile,
        "languages": ["English (fluent)", "Hindi (fluent)", "German (basic)"],
        "legal": {"citizenship": "Indian"},
        "preferences": {"notice_period": "Immediately", "earliest_start_date": "Immediately", "salary_expectations": {"INR": 1800000, "EUR": 40000, "USD": 40000}},
    }
    p = CandidateProfile.model_validate(profile)
    nodes = [
        SnapshotNode(i=0, role="textbox", name="What are your salary expectations for this role? (gross/annual - based on 40hrs/week)*", tag="input", type="text", required=True, visible=True),
        SnapshotNode(i=1, role="textbox", name="Expected CTC (INR)", tag="input", type="number", required=True, visible=True),
        SnapshotNode(i=2, role="textbox", name="Current CTC", tag="input", type="text", required=True, visible=True),
        SnapshotNode(i=3, role="textbox", name="Your Citizenship(s)*", tag="input", type="text", required=True, visible=True),
        SnapshotNode(i=4, role="textbox", name="Which language(s) can you write and speak fluently?*", tag="input", type="text", required=True, visible=True),
        SnapshotNode(i=5, role="textbox", name="What is your earliest possible start date for this role? (please use DD/MM/YYYY format)", tag="input", type="text", visible=True),
    ]
    by = {f.node: f for f in build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"], job_location="Düsseldorf, Germany").fields}
    assert (by[0].status, by[0].value) == ("auto", "40,000 EUR per year")
    assert (by[1].status, by[1].value) == ("auto", "1800000")
    assert by[2].status == "needs_input"  # current pay is not the expected pay
    assert by[3].value == "Indian"
    assert by[4].value == "English, Hindi"  # asks about fluency: German is basic
    import re as _re

    assert _re.fullmatch(r"\d\d/\d\d/\d{4}", by[5].value or "")
    no_loc = build_fill_plan(nodes[:1], p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields[0]
    assert (no_loc.status, no_loc.value) == ("auto", "40,000 USD per year")  # currency unknown: the candidate's own country's (this sample profile lives in the US), never a bare guess


def test_dropdown_answers_use_real_options(sample_profile) -> None:
    profile = {
        **sample_profile,
        "contact": {**sample_profile["contact"], "country": "India", "phone": "+91 9315605606"},
        "languages": ["English (fluent)", "Hindi (fluent)", "German (basic)"],
        "preferences": {"remote_types": ["remote", "hybrid", "onsite"], "salary_expectations": {"EUR": 40000}},
    }
    p = CandidateProfile.model_validate(profile)
    cb = lambda i, name, opts, **kw: SnapshotNode(i=i, role="combobox", name=name, tag="input", type="text", options=opts, required=True, visible=True, **kw)  # noqa: E731
    nodes = [
        cb(0, "What are your salary expectations for this role? (gross/annual - based on 40hrs/week)*", ["€30,000 - €39,999", "€40,000 - €49,999", "€50,000 - €59,999"]),
        cb(1, "Which language(s) can you write and speak fluently?*", ["English", "German", "Hindi", "Spanish"]),
        cb(2, "Stay in the know!*", ["Yes, keep me posted", "No, thanks"]),
        cb(3, "Are you currently based in the United States?", ["Yes", "No"]),
        cb(4, "Are you currently based in India?", ["Yes", "No"]),
        cb(5, "Our hybrid model requires talents to work from the office 3 days a week. Are you able to do so?*", ["Yes", "No"]),
        SnapshotNode(i=6, role="textbox", name="Phone", tag="input", type="tel", maxlength=10, required=True, visible=True),
        SnapshotNode(i=7, role="textbox", name="Mobile number", tag="input", type="tel", required=True, visible=True),
    ]
    by = {f.node: f for f in build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields}
    assert (by[0].status, by[0].value) == ("auto", "€40,000 - €49,999")
    assert (by[1].status, by[1].value) == ("auto", "English\nHindi")  # German is basic, not fluent
    assert (by[2].status, by[2].value) == ("auto", "No, thanks")
    assert (by[3].value, by[4].value) == ("No", "Yes")
    assert (by[5].status, by[5].value) == ("auto", "Yes")
    assert by[6].value == "9315605606"  # box fits only the national number
    assert by[7].value == "+91 9315605606"
    contact = SnapshotNode(i=9, role="textbox", name="Contact number", tag="input", type="text", visible=True)
    assert build_fill_plan([contact], p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields[0].value == "+91 9315605606"


def test_salary_by_city_conversion_and_referral(sample_profile) -> None:
    prefs = {"salary_expectations": {"INR": 1800000, "EUR": 40000, "USD": 40000, "GBP": 35000}, "salary_convert_missing_from_usd": True, "referral_source": "Company website"}
    p = CandidateProfile.model_validate({**sample_profile, "preferences": prefs})
    sal = SnapshotNode(i=0, role="spinbutton", name="What are your salary expectations?*", tag="input", type="number", required=True, visible=True)
    heard = SnapshotNode(i=1, role="textbox", name="How did you hear about us?", tag="input", type="text", visible=True)

    def plan(loc: str):
        return {f.node: f for f in build_fill_plan([sal, heard], p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"], job_location=loc).fields}

    london = plan("London")
    assert (london[0].status, london[0].value) == ("auto", "35000")
    assert london[1].value == "Company website"
    assert plan("Bengaluru, India")[0].value == "1800000"  # India keeps 18 LPA
    assert plan("Toronto, Canada")[0].value == "55000"  # converted from USD 40k
    assert plan("Somewhere")[0].value == "40000"  # unknown place: the candidate's own country's currency (this sample profile lives in the US)


def test_trivago_style_questions(sample_profile) -> None:
    prefs = {"salary_expectations": {"EUR": 40000}, "referral_source": "Company website"}
    p = CandidateProfile.model_validate({**sample_profile, "preferences": prefs, "legal": {"gender": "Decline to self-identify"}})
    cb = lambda i, name, opts: SnapshotNode(i=i, role="combobox", name=name, tag="input", type="text", options=opts, required=True, visible=True)  # noqa: E731
    nodes = [
        cb(0, "What are your salary expectations for this role? (gross/annual - based on 40hrs/week)*", ["25,000€", "35,000€", "40,000€", "45,000€"]),
        cb(1, "Which of the following most accurately describe you?*", ["Male", "Female", "Neutral", "Prefer to self-describe", "Prefer not to say"]),
        cb(2, "How did you hear about us?*", ["LinkedIn", "trivago Careers Page", "Referral", "Other"]),
    ]
    by = {f.node: f for f in build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields}
    assert (by[0].status, by[0].value) == ("auto", "40,000€")
    assert (by[1].status, by[1].value) == ("auto", "Prefer not to say")
    assert (by[2].status, by[2].value) == ("auto", "trivago Careers Page")


def test_graduated_and_us_only(sample_profile) -> None:
    p = CandidateProfile.model_validate({**sample_profile, "education": [{"school": "GGSIPU", "end_date": "2025-06"}]})
    nodes = [SnapshotNode(i=0, role="combobox", name="Have you graduated yet? We are only considering candidates who have already graduated.", tag="input", type="text", options=["Yes", "No"], required=True, visible=True)]
    f = build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields[0]
    assert (f.status, f.value) == ("auto", "Yes")
    from app.jobs.matching import US_ONLY_RE

    assert US_ONLY_RE.search("Are you based in the U.S.? We are only able to hire within the U.S. at this time.")
    assert eligibility_blockers({"description": "We are only able to hire within the U.S."}, {"legal": {"work_authorized_countries": ["India"]}})
    assert not US_ONLY_RE.search("Are you authorized to work in the US?")


def test_users_standard_answers(sample_profile) -> None:
    profile = {
        **sample_profile,
        "experience_years": 2,
        "contact": {**sample_profile["contact"], "first_name": "Manik", "last_name": "Goyal", "address_line1": "Sector 22", "city": "Gurugram", "state": "Haryana", "postal_code": "122015", "country": "India", "alternate_phone": "749733252", "permanent_address": "Omaxe, Bahadurgarh, Haryana 124507, India"},
        "preferences": {"marketing_opt_in": True, "why_join": "Looking for a more reputed and stable organization.", "reason_for_leaving": "Looking for a more reputed and stable organization."},
        "legal": {"gender": "Male", "veteran_status": "No", "disability_status": "No", "criminal_history": "No", "relatives_at_company": "No"},
    }
    p = CandidateProfile.model_validate(profile)
    t = lambda i, name: SnapshotNode(i=i, role="textbox", name=name, tag="input", type="text", required=True, visible=True)  # noqa: E731
    cb = lambda i, name, opts: SnapshotNode(i=i, role="combobox", name=name, tag="input", type="text", options=opts, required=True, visible=True)  # noqa: E731
    nodes = [
        t(0, "Why Granola?"),
        t(1, "What is your reason for leaving your current role?"),
        t(2, "How many years of experience do you have with Python?"),
        t(3, "Signature"),
        t(4, "Permanent Address"),
        t(5, "Current Address"),
        t(6, "Alternate phone number"),
        cb(7, "Stay in touch!*", ["Yes, keep me posted", "No, thanks"]),
        cb(8, "Can we consider you for other roles?", ["Yes", "No"]),
        cb(9, "Veteran Status*", ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]),
        cb(10, "Gender*", ["Male", "Female", "Decline to self-identify"]),
        cb(11, "Do you have any relatives or friends currently working at Acme?", ["Yes", "No"]),
        cb(12, "Disability status", ["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"]),
    ]
    by = {f.node: f for f in build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields}
    assert by[0].value.startswith("Looking for a more reputed")
    assert by[1].value.startswith("Looking for a more reputed")
    assert by[2].value == "2"
    assert by[3].value == "Manik Goyal"
    assert by[4].value.startswith("Omaxe, Bahadurgarh")
    assert by[5].value == "Sector 22, Gurugram, Haryana 122015, India"
    assert by[6].value == "749733252"
    assert by[7].value == "Yes, keep me posted" and by[8].value == "Yes"
    assert by[9].value == "I am not a protected veteran"
    assert by[10].value == "Male"
    assert by[11].value == "No"
    assert by[12].value == "No, I do not have a disability"
    assert all(by[i].status == "auto" for i in by), {i: (by[i].status, by[i].reason) for i in by if by[i].status != "auto"}


def test_which_country_and_dial_code(sample_profile) -> None:
    p = CandidateProfile.model_validate({**sample_profile, "contact": {**sample_profile["contact"], "country": "India"}})
    countries = ["Afghanistan", "Albania"] + [f"Country {i}" for i in range(40)]
    nodes = [SnapshotNode(i=0, role="combobox", name="In which Country do you currently live?*", tag="input", type="text", options=countries, required=True, visible=True)]
    f = build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields[0]
    assert (f.status, f.value) == ("auto", "India")  # typed & searched in the long list, never "No"
    assert values_match("select", "India", "+91")
    assert not values_match("select", "India", "+246")


def test_referral_fallback_experience_noncompete_privacy(sample_profile) -> None:
    prefs = {"referral_source": "Company website", "referral_fallback": "Google Job Search", "experienced_with": ["docker", "kubernetes", "containerization", "infrastructure as code", "ci/cd", "cloud", "azure", "aws"], "agree_to_privacy_notices": True}
    p = CandidateProfile.model_validate({**sample_profile, "preferences": prefs, "legal": {"non_compete": "No"}})
    cb = lambda i, name, opts: SnapshotNode(i=i, role="combobox", name=name, tag="input", type="text", options=opts, required=True, visible=True)  # noqa: E731
    nodes = [
        cb(0, "How did you hear about us?*", ["Google Job Search", "Job Board - LinkedIn", "Job Board - Stepstone"]),
        cb(1, "Do you have experience with containerization (e.g., Docker) and orchestration technologies (e.g., Kubernetes)?*", ["Yes", "No"]),
        cb(2, "Do you have an understanding of infrastructure as code, CI/CD pipelines, and cloud platforms (e.g., Azure, AWS)?*", ["Yes", "No"]),
        cb(3, "Are you now (or will you be) subject to any agreement containing restrictions or covenants regarding non-competition or non-solicitation that would restrict your ability to carry out the job for which you are applying?*", ["Yes", "No"]),
        cb(4, "Do you agree to the following personal data protection regulation?*", ["I agree", "I do not agree"]),
        cb(5, "By selecting yes to this question you confirm that you have read Esri’s Applicant Privacy Notice.*", ["Yes", "No"]),
        cb(6, "Do you have experience with Rust?", ["Yes", "No"]),
    ]
    by = {f.node: f for f in build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields}
    assert by[0].value == "Google Job Search"
    assert (by[1].value, by[2].value, by[3].value) == ("Yes", "Yes", "No")
    assert (by[4].value, by[5].value) == ("I agree", "Yes")
    assert by[6].status == "needs_input"  # not a confirmed topic: ask


def test_full_name_question_and_styled_resume_box(sample_profile) -> None:
    p = CandidateProfile.model_validate(sample_profile)
    resumes = [{"id": "r1", "label": "Default", "file_name": "cv.pdf"}]
    nodes = [
        SnapshotNode(i=16, role="textbox", name="What is your full name?*", tag="input", type="text", placeholder="Your full name*", required=True, visible=True),
        SnapshotNode(i=17, role="textbox", name="Resume*", tag="input", type="text", placeholder="Select the attachment", required=True, visible=True),
        SnapshotNode(i=30, role="textbox", name="Company name", tag="input", type="text", visible=True),
    ]
    by = {f.node: f for f in build_fill_plan(nodes, p.facts(), [], None, resumes=resumes, default_resume_id="r1", submit_names=["Apply"]).fields}
    assert by[16].status == "auto" and by[16].value == p.contact.full_name
    assert (by[17].field_type, by[17].status, by[17].resume_id) == ("file", "auto", "r1")
    assert by[30].value != p.contact.full_name
    act = FormFiller.action_for(by[17])
    assert act is not None and act.type == "upload"


def test_future_opportunities_checkbox_follows_opt_in(sample_profile) -> None:
    label = "Yes, Mendix can contact me about future job opportunities for up to 1 yearPrivacy Policy"
    node_ = SnapshotNode(i=0, role="checkbox", name=label, tag="input", type="checkbox", checked=False, visible=True)
    yes = CandidateProfile.model_validate({**sample_profile, "preferences": {"marketing_opt_in": True}})
    no = CandidateProfile.model_validate({**sample_profile, "preferences": {"marketing_opt_in": False}})
    f_yes = build_fill_plan([node_], yes.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields[0]
    f_no = build_fill_plan([node_], no.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields[0]
    assert (f_yes.status, f_yes.value) == ("auto", "Yes")
    assert (f_no.status, f_no.value) == ("auto", "No")


def test_personal_education_and_work_details(sample_profile) -> None:
    from datetime import date

    profile = {
        **sample_profile,
        "experience_years": 2,
        "contact": {**sample_profile["contact"], "first_name": "Manik", "middle_name": "", "last_name": "Goyal", "date_of_birth": "2003-09-23", "city": "Gurugram"},
        "education": [{"school": "Guru Gobind Singh Indraprastha University", "degree": "Bachelor of Technology (B.Tech)", "field_of_study": "Information Technology", "start_date": "2021-12-01", "end_date": "2025-06-01"}],
        "employment": [{"company": "I2V Systems Pvt. Ltd.", "title": "Software Engineer", "start_date": "2024-10-01", "current": True}],
        "legal": {"previously_employed_here": "No", "board_or_official": "No", "relatives_at_company": "No"},
    }
    p = CandidateProfile.model_validate(profile)
    t = lambda i, name, **kw: SnapshotNode(i=i, role="textbox", name=name, tag="input", type=kw.pop("type", "text"), visible=True, **kw)  # noqa: E731
    cb = lambda i, name, opts, **kw: SnapshotNode(i=i, role="combobox", name=name, tag="input", type="text", options=opts, visible=True, **kw)  # noqa: E731
    nodes = [
        t(0, "Date of Birth (DD/MM/YYYY)"),
        t(1, "Age"),
        cb(2, "Highest qualification", ["B.E./B.Tech", "M.Tech", "MBA"]),
        cb(3, "Field of study", ["Computer Science", "Information Technology", "Mechanical"]),
        t(4, "Start date", section="Work Experience"),
        t(5, "Start date", section="Education", type="month"),
        t(6, "End date", section="Education", placeholder="MM/YYYY"),
        cb(7, "Are you currently working?", ["Yes", "No"]),
        cb(8, "Do you have any work experience?", ["Yes", "No"]),
        cb(9, "Have you ever worked for Acme before?", ["Yes", "No"]),
        cb(10, "Are you a board member or director of any company?", ["Yes", "No"]),
        cb(11, "City", ["Delhi", "Gurgaon", "Noida"] + [f"C{i}" for i in range(3)]),
        cb(12, "Are you a fresher?", ["Yes", "No"]),
    ]
    by = {f.node: f for f in build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"]).fields}
    today = date.today()
    assert by[0].value == "23/09/2003"
    assert by[1].value == str(today.year - 2003 - ((today.month, today.day) < (9, 23)))
    assert by[2].value == "B.E./B.Tech" and by[3].value == "Information Technology"
    assert by[4].value == "01/10/2024"
    assert by[5].value == "2021-12" and by[6].value == "06/2025"
    assert (by[7].value, by[8].value, by[9].value, by[10].value) == ("Yes", "Yes", "No", "No")
    assert by[11].value == "Gurgaon"
    assert by[12].value == "No"


def test_clearance_roles_are_ineligible_without_us_work_rights() -> None:
    job = {"title": "Backend Software Engineer – TS/SCI", "description": "Active TS/SCI clearance required."}
    india = {"legal": {"work_authorized_countries": ["India"]}}
    assert eligibility_blockers(job, india)
    assert not eligibility_blockers(job, {"legal": {"work_authorized_countries": ["India", "United States"]}})
    assert not eligibility_blockers(job, {"legal": {}})  # unknown work rights: never guess
    assert not eligibility_blockers({"title": "Platform Engineer", "description": "Kubernetes"}, india)


def test_comeet_radio_questions_and_data_retention_consent(sample_profile) -> None:
    p = CandidateProfile.model_validate({**sample_profile, "legal": {**sample_profile.get("legal", {}), "previously_employed_here": "No"}, }, )
    p.legal.gender = "Male"
    p.preferences.marketing_opt_in = True
    worked = "Have you previously worked for or provided services to Cognyte or any of its ent"
    gender = "What gender identity do you most closely identify with?"
    keep = "I agree that you can keep my data for an extended time period so that it will be easier for you to contact me about job opportunities."
    nodes = [
        SnapshotNode(i=10, role="radio", name="Female", tag="input", type="radio", name_attr=gender, section=gender, checked=False, visible=True),
        SnapshotNode(i=11, role="radio", name="Male", tag="input", type="radio", name_attr=gender, section=gender, checked=False, visible=True),
        SnapshotNode(i=12, role="radio", name="Prefer not to say", tag="input", type="radio", name_attr=gender, section=gender, checked=False, visible=True),
        SnapshotNode(i=13, role="radio", name="Yes", tag="input", type="radio", name_attr=worked, section=worked, required=True, checked=False, visible=True),
        SnapshotNode(i=14, role="radio", name="No", tag="input", type="radio", name_attr=worked, section=worked, required=True, checked=False, visible=True),
        SnapshotNode(i=15, role="checkbox", name=keep, tag="input", type="checkbox", checked=False, visible=True),
    ]
    plan = build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"])
    by_label = {f.label[:20]: f for f in plan.fields}
    w = next(f for f in plan.fields if "previously worked" in f.label)
    assert (w.status, w.value) == ("auto", "No")
    g = next(f for f in plan.fields if "gender" in f.label.lower())
    assert g.status == "auto" and g.value == "Male"
    c = next(f for f in plan.fields if f.node == 15)
    assert c.status == "auto" and c.value in (True, "true", "checked", "Yes")
    assert by_label


def test_thank_you_shown_in_an_alert_box_is_a_confirmation() -> None:
    from app.browser.snapshot import PageSignals, PageSnapshot
    from app.browser.verifier import verify_submission
    from app.core.enums import VerificationStatus

    before = PageSnapshot(url="https://www.comeet.co/jobs/x/apply", title="Jobs", nodes=[])
    msg = "Thank you Manik! Your application has been submitted. Good luck!"
    after = PageSnapshot(url="https://www.comeet.co/jobs/x/apply", title="Jobs", nodes=[], text_excerpt=msg, signals=PageSignals(error_messages=[msg], success_messages=["application has been submitted"]))
    assert verify_submission(before, after).status == VerificationStatus.CONFIRMED
    bad = PageSnapshot(url=before.url, title="Jobs", nodes=[], signals=PageSignals(error_messages=["Please make sure to complete the required details"]))
    assert verify_submission(before, bad).status == VerificationStatus.REJECTED


def test_outage_page_is_retried_not_failed() -> None:
    from app.applications.workflow import _site_down
    from app.browser.snapshot import PageSnapshot

    down = PageSnapshot(url="https://alkira.bamboohr.com/careers/245", title="BambooHR temporarily unavailable", nodes=[SnapshotNode(i=0, role="link", name="View incident status", tag="a", visible=True)], text_excerpt="BambooHR temporarily unavailable. View incident status")
    assert _site_down(down)
    job = PageSnapshot(url="https://x.bamboohr.com/careers/1", title="Software Engineer - X", nodes=[SnapshotNode(i=0, role="button", name="Apply for this job", tag="button", visible=True)], text_excerpt="We are hiring. Service unavailable in your region is not a thing here.")
    assert not _site_down(job)


@pytest.mark.asyncio
async def test_settled_snapshot_waits_for_single_page_apps_to_draw_apply() -> None:
    from types import SimpleNamespace

    from app.applications.workflow import ApplicationWorkflow
    from app.browser.snapshot import PageSnapshot

    readings = iter([
        PageSnapshot(url="https://x.myworkdayjobs.com/job/1", title="", nodes=[SnapshotNode(i=0, role="link", name="Skip to main content", tag="a", visible=True)]),
        PageSnapshot(url="https://x.myworkdayjobs.com/job/1", title="Engineer", nodes=[SnapshotNode(i=0, role="link", name="Skip to main content", tag="a", visible=True), SnapshotNode(i=1, role="button", name="Apply", tag="a", href="https://x.myworkdayjobs.com/job/1/apply", visible=True)]),
    ])

    async def snapshot():
        return next(readings)

    ctx = SimpleNamespace(snapshot=snapshot)
    first = PageSnapshot(url="https://x.myworkdayjobs.com/job/1", title="", nodes=[])
    import app.applications.workflow as wf

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    orig = wf.asyncio.sleep
    wf.asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        snap = await ApplicationWorkflow._settled_snapshot(None, ctx, first)  # type: ignore[arg-type]
    finally:
        wf.asyncio.sleep = orig  # type: ignore[assignment]
    assert any(n.name == "Apply" for n in snap.nodes) and len(sleeps) == 2


def test_captcha_on_an_application_form_is_filled_not_refused() -> None:
    from app.browser.page_state import classify_page
    from app.browser.snapshot import PageSignals, PageSnapshot

    nodes = [
        SnapshotNode(i=0, role="textbox", name="First Name *", tag="input", type="text", visible=True),
        SnapshotNode(i=1, role="textbox", name="Email *", tag="input", type="email", visible=True),
        SnapshotNode(i=2, role="file", name="Resume *", tag="input", type="file", visible=True),
    ]
    form = PageSnapshot(url="https://x.bamboohr.com/careers/245", title="BambooHR", nodes=nodes, signals=PageSignals(has_captcha=True))
    assert classify_page(form).kind == "form"
    wall = PageSnapshot(url="https://x.example/verify", title="Verify", nodes=[], signals=PageSignals(has_captcha=True))
    assert classify_page(wall).kind == "challenge"


def test_dropdown_search_box_and_school_location(sample_profile) -> None:
    p = CandidateProfile.model_validate({**sample_profile, "education": [{"school": "GGSIPU", "degree": "B.Tech", "location": "Delhi, India"}]})
    nodes = [
        SnapshotNode(i=0, role="combobox", name="", tag="input", type="text", placeholder="Search by country/region or code", visible=True),
        SnapshotNode(i=1, role="combobox", name="School location", tag="input", type="text", visible=True),
        SnapshotNode(i=2, role="combobox", name="Institution*", tag="input", type="text", required=True, visible=True),
    ]
    plan = build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"])
    by = {f.node: f for f in plan.fields}
    assert by[0].status == "skip" and "search box" in by[0].reason
    assert (by[1].status, by[1].value) == ("auto", "Delhi, India")
    assert by[2].value == "GGSIPU"


def test_navigator_never_picks_a_submit_button_to_reach_a_form() -> None:
    from app.agent.navigator import _leads_to_applying
    from app.browser.snapshot import PageSnapshot

    snap = PageSnapshot(url="https://x.bamboohr.com/careers/245", title="Job", nodes=[])
    assert not _leads_to_applying(SnapshotNode(i=0, role="button", name="Submit Application", tag="button", visible=True), snap)
    assert _leads_to_applying(SnapshotNode(i=1, role="button", name="Apply for this Job", tag="button", visible=True), snap)


def test_existing_education_and_experience_entries_are_not_duplicated(sample_profile) -> None:
    p = CandidateProfile.model_validate({**sample_profile, "education": [{"school": "GGSIPU", "degree": "B.Tech"}], "employment": [{"company": "i2v", "title": "Engineer"}]})
    nodes = [
        SnapshotNode(i=0, role="button", name="Edit education Bachelor of Technology (B.Tech), GGSIP University", tag="button", visible=True),
        SnapshotNode(i=1, role="combobox", name="Institution*", tag="input", type="text", required=True, visible=True),
        SnapshotNode(i=2, role="textbox", name="Degree", tag="input", type="text", visible=True),
        SnapshotNode(i=3, role="button", name="Edit experience Software Engineer, i2v", tag="button", visible=True),
        SnapshotNode(i=4, role="textbox", name="Company", tag="input", type="text", section="Experience", visible=True),
        SnapshotNode(i=5, role="textbox", name="First name*", tag="input", type="text", required=True, visible=True),
    ]
    plan = build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"])
    by = {f.node: f for f in plan.fields}
    assert by[1].status == "skip" and by[2].status == "skip" and by[4].status == "skip"
    assert by[5].status == "auto" and by[5].value
