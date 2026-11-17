"""Tests for the Data tab: exporting the data files as a zip, and importing one back."""

import io
import zipfile
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from helpers import HTMX

from pledger.web.app import create_app
from pledger.web.store import LedgerStore


@pytest.fixture
def settings(settings, tmp_path):
    return replace(settings, budgets_path=tmp_path / "budgets.csv")


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def store(settings):
    return LedgerStore(settings)


def unzip(data: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name).decode() for name in zf.namelist()}


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buffer.getvalue()


def post_import(client, data: bytes, confirm=True):
    form = {"confirm": "true"} if confirm else {}
    return client.post("/data/import", files={"file": ("pledger.zip", data, "application/zip")}, data=form, headers=HTMX)


def ledger_text(settings) -> str:
    return settings.ledger_path.read_text(encoding="utf-8")


class TestExport:
    def test_every_existing_data_file_at_its_name_in_info(self, client, settings):
        response = client.get("/data/export")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert 'filename="pledger-' in response.headers["content-disposition"]
        files = unzip(response.content)
        assert set(files) == {
            "accounts.yaml", "result_types.yaml", "movements/ledger.csv", "currencies.yaml",
            "base_currency.csv", "currencies.csv",
        }
        assert files["movements/ledger.csv"] == ledger_text(settings)
        assert files["accounts.yaml"] == settings.accounts_path.read_text(encoding="utf-8")

    def test_the_api_too(self, client):
        response = client.get("/api/data/export")
        assert response.status_code == 200 and "accounts.yaml" in unzip(response.content)

    def test_one_file(self, client, settings):
        response = client.get("/data/files/movements/ledger.csv")
        assert response.status_code == 200 and response.text == ledger_text(settings)

    @pytest.mark.parametrize("name", ["budgets.csv", "nope.csv", "../accounts.yaml", "movements/../../etc/passwd"])
    def test_only_existing_data_files(self, client, name):
        assert client.get(f"/data/files/{name}").status_code == 404


class TestImport:
    def test_round_trip(self, client, store, settings):
        store.freeze_ids()  # as on startup, which the import does too
        exported = client.get("/data/export").content
        before = ledger_text(settings)
        store.add("2026-03-05", "Food", "Eq", "Ca", "30", "Lunch")
        changed = ledger_text(settings)
        assert changed != before

        response = post_import(client, exported)
        assert response.status_code == 200
        assert "Imported 6 files" in response.text
        assert ledger_text(settings) == before
        # The data before the import is in its backup, which can be imported to undo it
        [backup] = store.backups()
        assert backup.name.startswith("before-import-") and backup.name in response.text
        assert unzip(backup.path.read_bytes())["movements/ledger.csv"] == changed
        assert client.get(f"/data/backups/{backup.name}").content == backup.path.read_bytes()

    def test_files_missing_from_the_zip_are_removed(self, client, store, settings):
        exported = client.get("/data/export").content
        store.set_budget("2026-03", "Food", "40")
        assert settings.budgets_path.exists()
        response = post_import(client, exported)
        assert not settings.budgets_path.exists()
        assert "Removed, as they weren't in the zip: budgets.csv" in response.text

    def test_a_zipped_data_folder(self, client, settings):
        files = unzip(client.get("/data/export").content)
        files["movements/ledger.csv"] += "2026-03-05;Food;Eq;30;Ca;Lunch\n"
        post_import(client, make_zip({f"info/{name}": text for name, text in files.items()}))
        assert "Lunch" in ledger_text(settings)

    def test_unknown_entries_are_left_out(self, client):
        files = unzip(client.get("/data/export").content)
        response = post_import(client, make_zip({**files, "notes.txt": "hi", "__MACOSX/._accounts.yaml": "x"}))
        assert "Imported 6 files" in response.text
        assert "aren't data files of pledger: notes.txt" in response.text
        assert "__MACOSX" not in response.text

    def test_data_that_doesnt_load_is_refused(self, client, store, settings):
        files = unzip(client.get("/data/export").content)
        files["accounts.yaml"] = files["accounts.yaml"].replace("built_in:", "internal:")
        before = {f.name: f.path.read_bytes() for f in store.data_files() if f.size is not None}
        response = post_import(client, make_zip(files))
        assert "doesn&#39;t load, so nothing was imported" in response.text
        assert {f.name: f.path.read_bytes() for f in store.data_files() if f.size is not None} == before
        assert store.backups() == []

    @pytest.mark.parametrize(("data", "error"), [
        (b"not a zip", "isn&#39;t a zip"),
        (make_zip({"accounts.yaml": ""}), "missing the required files"),
        (make_zip({"accounts.yaml": "", "info/result_types.yaml": ""}), "missing the required files"),
    ])
    def test_not_an_export(self, client, store, data, error):
        assert error in post_import(client, data).text
        assert store.backups() == []

    def test_needs_the_confirmation(self, client, settings):
        exported = client.get("/data/export").content
        settings.ledger_path.write_text(ledger_text(settings) + "2026-03-05;Food;Eq;30;Ca;Lunch\n", encoding="utf-8")
        response = post_import(client, exported, confirm=False)
        assert "replaces yours to import it" in response.text and "Lunch" in ledger_text(settings)

    def test_the_api(self, client, settings):
        exported = client.get("/api/data/export").content
        response = client.post("/api/data/import", files={"file": ("pledger.zip", exported, "application/zip")})
        assert response.status_code == 200
        body = response.json()
        assert "movements/ledger.csv" in body["imported"] and body["backup"].endswith(".zip")
        bad = client.post("/api/data/import", files={"file": ("x.zip", b"nope", "application/zip")})
        assert bad.status_code == 422

    def test_refused_from_another_site(self, client):
        exported = client.get("/data/export").content
        response = client.post(
            "/data/import", files={"file": ("pledger.zip", exported, "application/zip")},
            data={"confirm": "true"}, headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403


class TestPage:
    def test_lists_the_data_files(self, client):
        page = client.get("/data").text
        assert 'href="/data/files/movements/ledger.csv"' in page
        assert "budgets.csv" in page  # missing, but listed
        assert "None yet" in page

    def test_works_when_the_ledger_doesnt_load(self, client, settings):
        settings.accounts_path.write_text(settings.accounts_path.read_text().replace("built_in:", "internal:"))
        assert client.get("/").status_code == 500
        assert client.get("/data").status_code == 200

    def test_in_the_top_bar(self, client):
        assert '<a href="/data" aria-current="page">Data</a>' in client.get("/data").text

    def test_unknown_backup(self, client):
        assert client.get("/data/backups/nope.zip").status_code == 404
