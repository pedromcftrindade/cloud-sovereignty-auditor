"""The web app (exitpilot/webapp.py): a real server on a free port, called like the browser does."""

import base64
import http.client
import io
import json
import threading
import zipfile
from urllib.parse import quote

import pytest

from exitpilot import webapp


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("LLM_PROVIDER", "ollama")
    mp.setenv("EXITPILOT_OFFLINE", "1")             # never call a model from the tests
    srv = webapp.make_server("127.0.0.1", 0)         # port 0: any free port
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()
    mp.undo()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp, data


def call_json(port, method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    resp, data = call(port, method, path, body, {"Content-Type": "application/json"} if body else {})
    return resp.status, json.loads(data)


def analyse(port, example="loja-lixo"):
    status, data = call_json(port, "POST", f"/api/analyse?example={example}&ai=0")
    assert status == 200, data
    return data


def test_binds_to_this_machine_by_default():
    import inspect
    assert inspect.signature(webapp.make_server).parameters["host"].default == "127.0.0.1"
    assert 'parser.add_argument("--host", default="127.0.0.1"' in inspect.getsource(webapp.main)


def test_status_lists_every_example_with_a_description(server):
    status, data = call_json(server, "GET", "/api/status")
    assert status == 200
    names = [e["name"] for e in data["examples"]]
    assert {"loja-mista", "loja-lixo", "loja-simples"} <= set(names)
    assert all(e["description"] for e in data["examples"])
    assert data["weights"] == webapp.DEFAULT_WEIGHTS and "Lisbon" in data["cities"]


def test_page_and_static_files_are_served(server):
    resp, html = call(server, "GET", "/")
    assert resp.status == 200 and b"/static/app.js" in html and b"integrity=" in html
    resp, js = call(server, "GET", "/static/app.js")
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/javascript")


@pytest.mark.parametrize("path", ["/static/../webapp.py", "/static/..%2Fwebapp.py", "/static/../../README.md",
                                  "/static/", "/static/nothing.js", "/etc/passwd"])
def test_no_file_outside_static_is_served(server, path):
    resp, body = call(server, "GET", path)
    assert resp.status == 404 and b"import" not in body


def test_analysis_market_migration_and_downloads(server):
    data = analyse(server)
    assert data["project"] == "loja-lixo" and data["header"]["score_rounded"] is not None
    assert data["security"] and data["suspected_waste"] and data["market"]["offers"]
    sid = data["id"]

    status, m = call_json(server, "POST", "/api/market", {"id": sid, "city": "Berlin",
                                                          "weights": {"price": 100, "automation": 0}})
    assert status == 200 and m["city"] == "Berlin" and m["weights"]["price"] == 100.0

    status, mg = call_json(server, "POST", "/api/migrate", {"id": sid, "exclude_waste": True})
    assert status == 200 and mg["steps"] and mg["files_content"]

    for what, ctype in [("report.md", "text/markdown"), ("analysis.json", "application/json"),
                        ("market.md", "text/markdown"), ("migration.zip", "application/zip"),
                        ("infrastructure.json", "application/json")]:
        resp, body = call(server, "GET", f"/api/download?id={sid}&what={what}")
        assert resp.status == 200 and resp.getheader("Content-Type").startswith(ctype), what
        expected = what if what == "infrastructure.json" else f"loja-lixo-{what}"
        assert resp.getheader("Content-Disposition").startswith(f'attachment; filename="{expected}"')
        assert body


def test_bad_requests_get_a_clear_error(server):
    assert call_json(server, "POST", "/api/analyse?example=../../etc")[0] == 400
    assert call_json(server, "POST", "/api/market", {"id": "nope"})[0] == 404
    resp, body = call(server, "POST", "/api/migrate", b"{not json", {"Content-Type": "application/json"})
    assert resp.status == 400 and json.loads(body)["error"] == "Invalid JSON."
    assert call_json(server, "GET", "/api/download?id=nope&what=report.md")[0] == 404
    sid = analyse(server, "loja-simples")["id"]
    assert call_json(server, "GET", f"/api/download?id={sid}&what=migration.zip")[0] == 404   # plan not made yet
    assert call_json(server, "GET", f"/api/download?id={sid}&what=../../.env")[0] == 400


def test_weights_that_are_not_numbers_are_ignored(server):
    sid = analyse(server, "loja-simples")["id"]
    status, m = call_json(server, "POST", "/api/market", {"id": sid, "weights": ["price", 100]})
    assert status == 200 and m["weights"] == webapp.DEFAULT_WEIGHTS
    status, m = call_json(server, "POST", "/api/market", {"id": sid, "weights": {"price": True, "coverage": 500}})
    assert status == 200 and m["weights"] == webapp.DEFAULT_WEIGHTS


def test_negative_content_length_is_refused_at_once(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=10)
    conn.putrequest("POST", "/api/migrate")
    conn.putheader("Content-Length", "-1")
    conn.endheaders()
    resp = conn.getresponse()                                   # would wait forever before the fix
    assert resp.status == 400
    conn.close()


def zip_of(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


TF = 'resource "aws_s3_bucket" "media" {\n  bucket = "media"\n}\n'


def test_zip_upload_with_an_unusual_name_downloads_fine(server):
    name = "Projeto final \"v2\" 🚀.zip"
    resp, body = call(server, "POST", "/api/analyse?ai=0&name=" + quote(name),
                      zip_of({"infra/main.tf": TF}), {"Content-Type": "application/zip"})
    data = json.loads(body)
    assert resp.status == 200, data
    resp, _ = call(server, "GET", f"/api/download?id={data['id']}&what=report.md")
    disposition = resp.getheader("Content-Disposition")
    assert resp.status == 200
    assert 'filename="Projeto-final-v2' in disposition and "filename*=UTF-8''Projeto%20final" in disposition
    assert "\n" not in disposition and "🚀" not in disposition


def test_upload_of_single_files_as_json(server):
    files = [{"name": "main.tf", "data": base64.b64encode(TF.encode()).decode()},
             {"name": "../../evil.tf", "data": base64.b64encode(TF.encode()).decode()},   # only the name is kept
             {"name": "infra.zip", "data": base64.b64encode(zip_of({"x/vars.tf": "variable \"a\" {}\n"})).decode()}]
    status, data = call_json(server, "POST", "/api/analyse?ai=0", {"files": files})
    assert status == 200 and any(r["type"] == "aws_s3_bucket" for r in data["ir"]["resources"])


@pytest.mark.parametrize("payload", [{"files": "main.tf"}, {"files": ["main.tf"]},
                                     {"files": [{"name": "main.tf", "data": "not base64!"}]}])
def test_malformed_uploads_get_a_400(server, payload):
    status, data = call_json(server, "POST", "/api/analyse?ai=0", payload)
    assert status == 400 and data["error"]


def test_zip_that_escapes_the_folder_is_refused(server):
    resp, body = call(server, "POST", "/api/analyse?ai=0&name=x.zip", zip_of({"../../outside.tf": TF}),
                      {"Content-Type": "application/zip"})
    assert resp.status == 400


def test_attachment_header_is_always_safe():
    assert webapp.attachment("loja-mista-report.md") == ("attachment; filename=\"loja-mista-report.md\"; "
                                                         "filename*=UTF-8''loja-mista-report.md")
    header = webapp.attachment('a"\r\nSet-Cookie: x=1.md')
    assert "\r" not in header and "\n" not in header and header.count('"') == 2
    assert webapp.attachment("🚀").startswith('attachment; filename="download"')
