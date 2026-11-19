#!/usr/bin/env python3
"""End-to-end verification for chat file attachments.

Start the stack first:
    docker compose up --build

Then run:
    python scripts/attachment_test.py

It checks upload, send, delivery, download by both participants, outsider
denial, resend of a failed file, chat-delete hiding and rejection of uploads
from synthetic (internal-token) traffic.
"""

from __future__ import annotations

import os
import time
import uuid

import requests

GATEWAY = os.getenv("PINGHUB_GATEWAY_URL", "http://localhost:8000")
NOTIFICATION = os.getenv("PINGHUB_NOTIFICATION_URL", "http://localhost:8002")
SIGNUP_CODE = os.getenv("PINGHUB_SIGNUP_CODE", "pinghub-demo")
INTERNAL_TOKEN = os.getenv("PINGHUB_INTERNAL_TOKEN", "pinghub-internal-dev")

results: list[tuple[bool, str, str]] = []


def record(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -> {detail}" if detail else ""))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register(username: str) -> str:
    response = requests.post(
        f"{GATEWAY}/auth/register",
        json={
            "username": username,
            "password": "Passw0rd!23",
            "signup_code": SIGNUP_CODE,
            "display_name": username.title(),
        },
        timeout=20,
    )
    assert response.status_code in (200, 201), (response.status_code, response.text)
    return response.json()["token"]


def upload(token: str, filename: str, content: bytes, content_type: str) -> str | None:
    response = requests.post(
        f"{GATEWAY}/attachments",
        files={"file": (filename, content, content_type)},
        headers=bearer(token),
        timeout=30,
    )
    return response.json().get("id") if response.status_code == 201 else None


def wait_task(task_id: str | None, token: str, timeout: float = 25) -> dict:
    if not task_id:
        return {}
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        response = requests.get(
            f"{GATEWAY}/messages/{task_id}", headers=bearer(token), timeout=10
        )
        if response.ok:
            last = response.json()
            if last.get("status") in ("DELIVERED", "FAILED"):
                return last
        time.sleep(0.4)
    return last


def finish() -> None:
    passed = sum(1 for ok, _, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed.")
    if passed != len(results):
        raise SystemExit(1)


def main() -> None:
    suffix = uuid.uuid4().hex[:6]
    alice, bob, carol = f"att_a{suffix}", f"att_b{suffix}", f"att_c{suffix}"

    control = requests.post(
        f"{NOTIFICATION}/control", json={"mode": "always_succeed"}, timeout=10
    )
    record(control.ok, "provider set to always_succeed", str(control.status_code))

    ta, tb, tc = register(alice), register(bob), register(carol)
    record(True, "three employees registered", f"{alice}, {bob}, {carol}")
    ha, hb, hc = bearer(ta), bearer(tb), bearer(tc)

    # Unauthenticated / synthetic uploads are rejected.
    response = requests.post(
        f"{GATEWAY}/attachments",
        files={"file": ("a.txt", b"x", "text/plain")},
        timeout=10,
    )
    record(response.status_code == 401, "upload without a session rejected",
           f"status={response.status_code}")

    response = requests.post(
        f"{GATEWAY}/attachments",
        files={"file": ("a.txt", b"x", "text/plain")},
        headers={"X-Internal-Token": INTERNAL_TOKEN},
        timeout=10,
    )
    record(response.status_code == 403, "internal-token upload rejected",
           f"status={response.status_code}")

    # Upload a file as alice.
    content = b"Hello attachment\nline two\n"
    response = requests.post(
        f"{GATEWAY}/attachments",
        files={"file": ("report.txt", content, "text/plain")},
        headers=ha,
        timeout=30,
    )
    record(response.status_code == 201, "upload .txt as alice",
           f"status={response.status_code} {response.text[:120]}")
    if response.status_code != 201:
        return finish()

    meta = response.json()
    attachment_id = meta["id"]
    record(
        meta["filename"] == "report.txt" and meta["size_bytes"] == len(content),
        "upload metadata is correct",
        str(meta),
    )

    # An unsent draft belongs only to its uploader.
    response = requests.get(f"{GATEWAY}/attachments/{attachment_id}", headers=hc, timeout=10)
    record(response.status_code == 403, "outsider cannot read draft metadata",
           f"status={response.status_code}")

    # File-only message (empty body).
    response = requests.post(
        f"{GATEWAY}/messages",
        json={"sender": alice, "receiver": bob, "body": "", "attachment_id": attachment_id},
        headers=ha,
        timeout=15,
    )
    record(response.status_code == 202, "file-only message accepted",
           f"status={response.status_code} {response.text[:120]}")
    task_id = response.json().get("task_id") if response.status_code == 202 else None
    if not task_id:
        return finish()

    state = wait_task(task_id, ta)
    record(state.get("status") == "DELIVERED", "file message delivered",
           f"status={state.get('status')} attempts={state.get('attempts')}")

    # Both participants see the attachment in their thread.
    for label, me, peer, headers in (
        ("alice", alice, bob, ha),
        ("bob", bob, alice, hb),
    ):
        response = requests.get(
            f"{GATEWAY}/users/{me}/conversations/{peer}", headers=headers, timeout=10
        )
        attachments = [
            message["attachment"]
            for message in response.json().get("messages", [])
            if message.get("attachment")
        ]
        record(
            bool(attachments) and attachments[0]["id"] == attachment_id,
            f"{label} thread shows the attachment",
            str(attachments[:1]),
        )

    # Both participants can download the original bytes.
    for label, headers in (("alice", ha), ("bob", hb)):
        response = requests.get(
            f"{GATEWAY}/attachments/{attachment_id}/content", headers=headers, timeout=30
        )
        disposition = response.headers.get("Content-Disposition", "")
        nosniff = response.headers.get("X-Content-Type-Options", "")
        record(
            response.status_code == 200
            and response.content == content
            and "attachment" in disposition
            and nosniff == "nosniff",
            f"{label} downloads the original bytes",
            f"status={response.status_code} bytes={len(response.content)} "
            f"cd={disposition[:40]} nosniff={nosniff}",
        )

    # An outsider cannot read the bytes.
    response = requests.get(
        f"{GATEWAY}/attachments/{attachment_id}/content", headers=hc, timeout=10
    )
    record(response.status_code == 403, "outsider cannot download",
           f"status={response.status_code}")

    # A delivered file cannot be attached to a second message.
    response = requests.post(
        f"{GATEWAY}/messages",
        json={"sender": alice, "receiver": bob, "body": "again", "attachment_id": attachment_id},
        headers=ha,
        timeout=10,
    )
    record(response.status_code == 409, "reuse of a delivered file rejected",
           f"status={response.status_code} {response.text[:120]}")

    # Text + file in the same message still works.
    second = upload(ta, "notes.pdf", b"%PDF-1.4 fake", "application/pdf")
    record(bool(second), "upload a different file type", f"id={second}")
    response = requests.post(
        f"{GATEWAY}/messages",
        json={"sender": alice, "receiver": bob, "body": "see attached", "attachment_id": second},
        headers=ha,
        timeout=15,
    )
    record(response.status_code == 202, "text + file message accepted",
           f"status={response.status_code}")

    # A failed file message can be resent with the same attachment id.
    requests.post(f"{NOTIFICATION}/control", json={"mode": "always_fail"}, timeout=10)
    third = upload(ta, "failed.csv", b"a,b\n1,2\n", "text/csv")
    response = requests.post(
        f"{GATEWAY}/messages",
        json={"sender": alice, "receiver": bob, "body": "", "attachment_id": third},
        headers=ha,
        timeout=15,
    )
    failed_task = response.json().get("task_id") if response.status_code == 202 else None
    failed_state = wait_task(failed_task, ta)
    record(failed_state.get("status") == "FAILED", "file message fails while provider is down",
           f"status={failed_state.get('status')}")

    requests.post(f"{NOTIFICATION}/control", json={"mode": "always_succeed"}, timeout=10)
    response = requests.post(
        f"{GATEWAY}/messages",
        json={"sender": alice, "receiver": bob, "body": "", "attachment_id": third},
        headers=ha,
        timeout=15,
    )
    record(response.status_code == 202, "failed file resent with the same attachment",
           f"status={response.status_code} {response.text[:120]}")
    resent_task = response.json().get("task_id") if response.status_code == 202 else None
    resent_state = wait_task(resent_task, ta)
    record(resent_state.get("status") == "DELIVERED", "resent file message delivered",
           f"status={resent_state.get('status')}")

    # Deleting a chat hides its files for that user only.
    response = requests.delete(
        f"{GATEWAY}/users/{alice}/conversations/{bob}", headers=ha, timeout=10
    )
    record(response.ok, "alice deleted the chat", f"status={response.status_code}")
    response = requests.get(
        f"{GATEWAY}/attachments/{attachment_id}/content", headers=ha, timeout=10
    )
    record(response.status_code == 404, "deleted chat hides the file for alice",
           f"status={response.status_code}")
    response = requests.get(
        f"{GATEWAY}/attachments/{attachment_id}/content", headers=hb, timeout=10
    )
    record(response.status_code == 200, "bob still has his copy",
           f"status={response.status_code}")

    finish()


if __name__ == "__main__":
    main()
