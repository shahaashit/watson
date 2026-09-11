"""Scratch notes taken from the work boards must survive a restart."""


def test_notes_round_trip_through_create_edit_and_delete(client):
    created = client.post("/api/notes", json={"body": "Ask about the migration window"})
    note = created.json()["note"]

    edited = client.patch(f"/api/notes/{note['id']}", json={"body": "Ask about staging too"})
    listed = client.get("/api/notes").json()["notes"]
    removed = client.delete(f"/api/notes/{note['id']}")

    assert created.status_code == 201
    assert edited.json()["note"]["body"] == "Ask about staging too"
    assert [item["body"] for item in listed] == ["Ask about staging too"]
    assert removed.status_code == 200
    assert client.get("/api/notes").json()["notes"] == []


def test_notes_reject_empty_bodies_and_missing_ids(client):
    assert client.post("/api/notes", json={"body": "   "}).status_code == 201
    assert client.post("/api/notes", json={"body": ""}).status_code == 422
    assert client.patch("/api/notes/9999", json={"body": "gone"}).status_code == 404
    assert client.delete("/api/notes/9999").status_code == 404


def test_notes_list_newest_first_and_hold_position_when_edited(client):
    first = client.post("/api/notes", json={"body": "First"}).json()["note"]
    client.post("/api/notes", json={"body": "Second"})
    client.patch(f"/api/notes/{first['id']}", json={"body": "First, revisited"})

    assert [note["body"] for note in client.get("/api/notes").json()["notes"]] == [
        "Second", "First, revisited",
    ]
