import json

import pytest
from fastapi.testclient import TestClient

import app as server
from auth import AccessConfig

EMPLOYEE = "arjun.employee@qorvexa.com"
MANAGER = "priya.manager@qorvexa.com"
ADMIN = "hr.admin@qorvexa.com"


@pytest.fixture(scope="module", autouse=True)
def restrict_manager_doc():
    server.store.set_document_groups("Manager_Playbook.pdf", ["managers"])


def client_for(email=None):
    c = TestClient(server.app)
    if email:
        assert c.post("/auth/dev-login", json={"email": email}).status_code == 200
    return c


def ask(c, question, history=()):
    res = c.post("/api/ask", json={"question": question, "history": list(history)})
    assert res.status_code == 200
    events = {}
    for block in res.text.strip().split("\n\n"):
        name = block.split("\n")[0].removeprefix("event: ")
        data = json.loads(block.split("\n")[1].removeprefix("data: "))
        events[name] = events.get(name, "") + data if name == "token" else data
    return events


def test_requires_sign_in():
    c = client_for()
    assert c.post("/api/ask", json={"question": "leave policy"}).status_code == 401
    assert c.get("/api/me").json()["user"] is None


def test_rejects_outside_domain():
    c = client_for()
    assert c.post("/auth/dev-login", json={"email": "someone@gmail.com"}).status_code == 403


def test_employee_cannot_see_restricted_document():
    c = client_for(EMPLOYEE)
    docs = {d["name"] for d in c.get("/api/status").json()["documents"]}
    assert docs == {"Qorvexa_HR_Handbook.pdf"}
    for q in ["How do I apply for leave?", "Can I accept gifts from vendors?", "probation"]:
        sources = ask(c, q)["sources"]
        assert sources and all(s["source"] == "Qorvexa_HR_Handbook.pdf" for s in sources)


def test_manager_and_admin_can_see_restricted_document():
    for email in (MANAGER, ADMIN):
        c = client_for(email)
        docs = {d["name"] for d in c.get("/api/status").json()["documents"]}
        assert "Manager_Playbook.pdf" in docs


def test_feedback_only_by_asker():
    c = client_for(EMPLOYEE)
    qid = ask(c, "How much notice do I give when quitting?")["done"]["id"]
    assert c.post("/api/feedback", json={"id": qid, "helpful": False}).status_code == 200
    other = client_for(MANAGER)
    assert other.post("/api/feedback", json={"id": qid, "helpful": True}).status_code == 404


def test_unanswered_questions_reach_insights():
    c = client_for(EMPLOYEE)
    events = ask(c, "zxqv plugh frobnicate")
    assert events["done"]["answered"] is False
    assert events["token"].startswith("I couldn't find this in the HR handbook.")
    assert c.get("/api/insights").status_code == 403

    # An off-topic follow-up must not borrow matches from the previous question.
    prev = [{"role": "user", "content": "Can I accept gifts from vendors?"}]
    followup = ask(c, "who won the cricket match yesterday", prev)
    assert followup["sources"] == [] and followup["done"]["answered"] is False

    data = client_for(ADMIN).get("/api/insights").json()
    reasons = {g["question"]: g["reason"] for g in data["gaps"]}
    assert reasons["zxqv plugh frobnicate"] == "not_in_handbook"
    assert reasons["How much notice do I give when quitting?"] == "unhelpful"


def test_upload_is_admin_only_and_sets_access():
    pdf = (server.DATA_DIR / "Qorvexa_HR_Handbook.pdf").read_bytes()
    files = {"file": ("Travel Policy.pdf", pdf, "application/pdf")}
    assert client_for(EMPLOYEE).post("/api/upload", files=files, data={"groups": "everyone"}).status_code == 403

    admin = client_for(ADMIN)
    assert admin.post("/api/upload", files=files, data={"groups": "nobody"}).status_code == 400
    res = admin.post("/api/upload", files=files, data={"groups": "hr-admin"})
    assert res.status_code == 200 and res.json()["name"] == "Travel_Policy.pdf"
    assert "Travel_Policy.pdf" not in {d["name"] for d in client_for(EMPLOYEE).get("/api/status").json()["documents"]}

    assert admin.put("/api/documents/Travel_Policy.pdf/access", json={"groups": ["everyone"]}).status_code == 200
    assert "Travel_Policy.pdf" in {d["name"] for d in client_for(EMPLOYEE).get("/api/status").json()["documents"]}


def test_group_wildcards(tmp_path):
    cfg = tmp_path / "access.json"
    cfg.write_text(json.dumps({"groups": {"hr-admin": ["*@hr.qorvexa.com"]}, "allowed_domains": ["qorvexa.com"]}))
    access = AccessConfig(cfg)
    assert "hr-admin" in access.groups_for("Meera@HR.qorvexa.com")
    assert access.groups_for("dev@qorvexa.com", idp_groups=["engineering"]) == {"everyone", "engineering"}
    assert not access.domain_allowed("x@evil.com")


def test_sso_callback(monkeypatch):
    """Simulates the identity provider's reply; no real IdP needed."""
    class FakeIdP:
        userinfo = {}

        async def authorize_access_token(self, request):
            return {"userinfo": self.userinfo}

    idp = FakeIdP()
    monkeypatch.setattr(server, "AUTH_MODE", "oidc")
    monkeypatch.setattr(server.oidc, "client", idp)
    c = client_for()

    idp.userinfo = {"email": "Meera@qorvexa.com", "email_verified": True, "name": "Meera", "groups": ["managers"]}
    res = c.get("/auth/callback", follow_redirects=False)
    assert res.status_code == 307 and res.headers["location"] == "/#ask"
    user = c.get("/api/me").json()["user"]
    assert user["email"] == "meera@qorvexa.com" and "managers" in user["groups"]
    assert c.post("/auth/dev-login", json={"email": ADMIN}).status_code == 404  # dev sign-in off under SSO

    idp.userinfo = {"email": "intruder@gmail.com", "email_verified": True}
    assert client_for().get("/auth/callback").status_code == 403
    idp.userinfo = {"email": "x@qorvexa.com", "email_verified": False}
    assert client_for().get("/auth/callback").status_code == 403
