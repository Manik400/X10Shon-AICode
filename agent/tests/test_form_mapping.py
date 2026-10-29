from __future__ import annotations

from app.applications.field_mapping import build_fill_plan, heuristic_map_fields
from app.applications.profile import CandidateProfile
from app.browser.snapshot import SnapshotNode
from app.core.enums import Provenance


def node(i: int, name: str, type_: str = "text", role: str = "textbox", **kw) -> SnapshotNode:
    return SnapshotNode(i=i, role=role, name=name, tag="input", type=type_, visible=True, **kw)


def make_plan(nodes, profile, answers=None, resumes=None):
    p = CandidateProfile.model_validate(profile)
    return build_fill_plan(nodes, p.facts(), answers or [], None, resumes=resumes or [], default_resume_id=p.default_resume_id, submit_names=["Submit application", "Submit"])


def test_contact_fields_auto_filled_with_provenance(sample_profile) -> None:
    nodes = [node(0, "First name *", required=True), node(1, "Last Name", required=True), node(2, "Email", "email"), node(3, "Phone", "tel"), node(4, "LinkedIn profile", "url")]
    plan = make_plan(nodes, sample_profile)
    by = {f.node: f for f in plan.fields}
    assert by[0].status == "auto" and by[0].value == "Ada" and by[0].provenance == Provenance.VERIFIED_FACT
    assert by[1].value == "Testperson"
    assert by[2].value == "ada.testperson@example.test"
    assert by[4].value == "https://linkedin.com/in/ada-test"


def test_legal_questions_require_approved_fact(sample_profile) -> None:
    nodes = [
        node(0, "Are you legally authorized to work in the United States?", "select", "combobox", options=["Select…", "Yes", "No"], required=True),
        node(1, "Do you have a disability?", "select", "combobox", options=["Yes", "No", "Prefer not to say"], required=True),
    ]
    plan = make_plan(nodes, sample_profile)
    by = {f.node: f for f in plan.fields}
    assert by[0].status == "auto" and by[0].value == "Yes" and by[0].legally_significant
    assert by[1].status == "needs_input" and by[1].legally_significant


def test_work_authorization_and_sponsorship_are_answered_per_country(sample_profile) -> None:
    profile = {**sample_profile, "legal": {"work_authorized_countries": ["India"], "requires_sponsorship": "Yes", "work_authorization": "Yes"}}
    nodes = [
        node(0, "Are you legally authorized to work in the United States?", "select", "combobox", options=["Yes", "No"], required=True),
        node(1, "Are you legally authorized to work in India?", "select", "combobox", options=["Yes", "No"], required=True),
        node(2, "Will you now or in the future require visa sponsorship from Esri for employment (e.g., H-1B visa)?", "select", "combobox", options=["Yes", "No"], required=True),
        node(3, "Will you require sponsorship to work in India?", "select", "combobox", options=["Yes", "No"], required=True),
        node(4, "Are you authorized to work in the country where this job is located?", "select", "combobox", options=["Yes", "No"], required=True),
    ]
    by = {f.node: f for f in make_plan(nodes, profile).fields}
    assert (by[0].value, by[0].status) == ("No", "auto")
    assert (by[1].value, by[1].status) == ("Yes", "auto")
    assert (by[2].value, by[2].status) == ("Yes", "auto")  # H-1B implies the US: outside India -> needs sponsorship
    assert (by[3].value, by[3].status) == ("No", "auto")
    assert by[4].status == "needs_input"  # no country in the question and no job location given
    p = CandidateProfile.model_validate(profile)
    with_loc = build_fill_plan([nodes[4]], p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"], job_location="Bengaluru, India")
    assert with_loc.fields[0].value == "Yes"


def test_approved_answer_for_one_country_never_answers_another(sample_profile) -> None:
    answers = [{"id": "w1", "category": "legal", "question": "Are you legally authorized to work in India?", "answer": "Yes", "provenance": "approved_answer", "legally_significant": True}]
    nodes = [node(0, "Are you legally authorized to work in the United States?", "select", "combobox", options=["Yes", "No"], required=True)]
    plan = make_plan(nodes, {**sample_profile, "legal": {}}, answers)
    assert plan.fields[0].status == "needs_input" and plan.fields[0].value is None


def test_sensitive_optional_question_without_answer_pauses(sample_profile) -> None:
    nodes = [node(0, "What are your salary expectations (EUR)?", required=False)]
    plan = make_plan(nodes, {**sample_profile, "preferences": {}})
    assert plan.fields[0].status == "needs_input"


def test_approved_answer_reused_for_custom_question(sample_profile) -> None:
    answers = [{"id": "a1", "category": "custom", "question": "How did you hear about this job?", "answer": "LinkedIn", "provenance": "approved_answer"}]
    plan = make_plan([node(0, "How did you hear about this job?")], sample_profile, answers)
    f = plan.fields[0]
    assert f.status == "auto" and f.value == "LinkedIn" and f.answer_id == "a1" and f.provenance == Provenance.APPROVED_ANSWER


def test_model_mapping_cannot_invent_values(sample_profile) -> None:
    p = CandidateProfile.model_validate(sample_profile)
    nodes = [node(0, "Favourite editor")]
    model = [{"node": 0, "profile_key": "contact.first_name", "answer_id": None, "value": "Vim", "confidence": 0.99, "category": "other"}]
    plan = build_fill_plan(nodes, p.facts(), [], model, resumes=[], default_resume_id=None, submit_names=["Submit"])
    f = plan.fields[0]
    assert f.value == "Ada"  # value copied from the fact, never from the model
    assert f.status == "needs_review"  # model-sourced → review


def test_select_value_without_matching_option_needs_review(sample_profile) -> None:
    nodes = [node(0, "Country", "select", "combobox", options=["Germany", "France"])]
    plan = make_plan(nodes, sample_profile)
    assert plan.fields[0].status == "needs_review"


def test_resume_upload_uses_default_resume(sample_profile) -> None:
    resumes = [{"id": "r1", "label": "Default", "file_name": "ada.pdf"}, {"id": "r2", "label": "Other", "file_name": "other.pdf"}]
    nodes = [node(0, "Resume/CV", "file", "file", required=True), node(1, "Cover Letter", "file", "file")]
    plan = make_plan(nodes, {**sample_profile, "default_resume_id": "r2"}, resumes=resumes)
    by = {f.node: f for f in plan.fields}
    assert by[0].status == "auto" and by[0].resume_id == "r2"
    assert by[1].status == "skip"


def test_consent_checkbox_needs_review_unless_approved(sample_profile) -> None:
    nodes = [node(0, "I consent to the processing of my data", "checkbox", "checkbox", required=True, checked=False)]
    assert make_plan(nodes, sample_profile).fields[0].status == "needs_review"
    answers = [{"id": "c1", "category": "consent", "question": "I consent to the processing of my data", "answer": "Yes", "provenance": "approved_answer"}]
    f = make_plan(nodes, sample_profile, answers).fields[0]
    assert f.status == "auto" and f.value == "Yes"


def test_radio_group_planned_once(sample_profile) -> None:
    answers = [{"id": "s1", "category": "sponsorship", "question": "Will you require sponsorship now or in the future?", "answer": "No", "provenance": "approved_answer"}]
    nodes = [
        node(0, "Yes", "radio", "radio", name_attr="cards[sponsorship]", section="Will you require sponsorship now or in the future?", checked=False),
        node(1, "No", "radio", "radio", name_attr="cards[sponsorship]", section="Will you require sponsorship now or in the future?", checked=False),
    ]
    plan = make_plan(nodes, sample_profile, answers)
    assert plan.fields[0].status == "auto" and plan.fields[0].value == "No"
    assert plan.fields[1].status == "skip"


def test_choice_buttons_answer_work_authorization_by_job_country(sample_profile) -> None:
    profile = {**sample_profile, "legal": {"work_authorized_countries": ["India"], "requires_sponsorship": "Yes"}}
    q = "Do you have work authorization to work in that country?"
    nodes = [
        SnapshotNode(i=5, role="radio", name="Yes", tag="button", type="radio", name_attr="choice:abc", section=q, required=True, checked=False, visible=True),
        SnapshotNode(i=6, role="radio", name="No", tag="button", type="radio", name_attr="choice:abc", section=q, required=True, checked=False, visible=True),
    ]
    p = CandidateProfile.model_validate(profile)
    plan = build_fill_plan(nodes, p.facts(), [], None, resumes=[], default_resume_id=None, submit_names=["Submit"], job_location="San Francisco, California, United States")
    f = plan.fields[0]
    assert (f.status, f.value, f.option_nodes) == ("auto", "No", {"Yes": 5, "No": 6})
    from app.applications.form_filler import FormFiller

    act = FormFiller.action_for(f)
    assert act is not None and act.target.strategy == "node" and act.target.value == "6"


def test_decline_answer_matches_any_decline_wording(sample_profile) -> None:
    profile = {**sample_profile, "legal": {"gender": "Decline to self-identify"}}
    nodes = [node(0, "Gender", "select", "combobox", options=["Male", "Female", "I don't wish to answer"], required=False)]
    f = make_plan(nodes, profile).fields[0]
    assert f.value == "I don't wish to answer"


def test_submit_and_next_detection(sample_profile) -> None:
    nodes = [node(0, "First name"), SnapshotNode(i=1, role="button", name="Next", tag="button", type="button"), SnapshotNode(i=2, role="button", name="Submit application", tag="button", type="submit")]
    plan = make_plan(nodes, sample_profile)
    assert plan.next_node == 1 and plan.submit_node == 2


def test_heuristic_mapper_only_uses_known_keys() -> None:
    out = heuristic_map_fields([{"node": 0, "label": "Email", "type": "email"}], ["contact.first_name"], [])
    assert out[0]["profile_key"] is None and out[0]["confidence"] < 0.5


def test_closest_option_by_shared_words() -> None:
    from app.applications.field_mapping import _choose_option

    opts = ["Career Site", "Company Website", "Job Board", "Employee Referral", "Social Media", "Other"]
    assert _choose_option(opts, "Company careers website") == "Company Website"
    assert _choose_option(["Job Board", "Social Media", "Other"], "LinkedIn") is None  # nothing shared: not guessed
    assert _choose_option(["LinkedIn", "Indeed", "Naukri"], "LinkedIn job post") == "LinkedIn"
    assert _choose_option(["Yes", "No"], "No") == "No"
    assert _choose_option(["Website A", "Website B"], "company website") is None  # a tie is not a pick


def test_workday_phone_block_national_number_and_device_type() -> None:
    from app.applications.field_mapping import build_fill_plan
    from app.browser.snapshot import SnapshotNode
    from app.core.enums import Provenance

    facts = {"contact.phone": ("+91 9315605606", Provenance.VERIFIED_FACT), "contact.email": ("m@example.com", Provenance.VERIFIED_FACT)}
    nodes = [
        SnapshotNode(i=10, role="combobox", name="Phone Device Type*", tag="button", options=["Select One", "Mobile", "Home", "Work"], required=True, visible=True),
        SnapshotNode(i=12, role="listbox", name="items selected", tag="ul", visible=True),
        SnapshotNode(i=13, role="combobox", name="Country Phone Code*", tag="input", type="text", value="India (+91)", visible=True),
        SnapshotNode(i=16, role="textbox", name="Phone Number*", tag="input", type="text", required=True, visible=True),
        SnapshotNode(i=17, role="textbox", name="Phone Extension", tag="input", type="text", visible=True),
    ]
    plan = build_fill_plan(nodes, facts, [], None, resumes=[], default_resume_id=None, submit_names=["Submit"])
    by = {f.node: f for f in plan.fields}
    assert by[16].value == "9315605606" and by[16].status == "auto"
    assert by[10].value == "Mobile" and by[10].status == "auto"
    assert by[17].status != "auto" or not by[17].value
