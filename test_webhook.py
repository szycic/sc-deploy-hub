"""GitHub Webhook Push Event Simulator for sc-deploy-hub.

Generates and signs a simulated GitHub push event webhook payload, then sends it
to the sc-deploy-hub controller endpoint at http://localhost:8000/api/v1/webhook.
Used for local testing and debugging webhook integration.
"""

import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request


def main() -> None:
    """Parse command line args and dispatch a signed mock push event."""
    if len(sys.argv) < 3:
        print("Usage: python3 test_webhook.py <repo_name> <branch_name> [secret]")
        print("Example: python3 test_webhook.py sc-deploy-hub main super-secret-webhook-key")
        sys.exit(1)

    repo_name = sys.argv[1]
    branch_name = sys.argv[2]
    secret = sys.argv[3] if len(sys.argv) > 3 else None

    url = "http://localhost:8000/api/v1/webhook"

    payload = {
        "ref": f"refs/heads/{branch_name}",
        "repository": {
            "name": repo_name,
            "full_name": f"szycic/{repo_name}",
        },
        "head_commit": {
            "id": "e6f8a2c4b8d10f12c3e456789abcde0123456789",
            "message": "Simulated release - trigger automatic deployment",
            "timestamp": "2026-07-17T09:35:00+02:00",
            "author": {
                "name": "Szymon",
                "email": "szymon@example.com",
            },
        },
        "pusher": {
            "name": "szymon",
        },
    }

    body = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
    }

    if secret:
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        headers["X-Hub-Signature-256"] = signature
        print(f"Computed HMAC Signature: {signature}")

    print(f"Sending push event for repo '{repo_name}' branch '{branch_name}' to {url}...")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as res:
            print(f"Response Code: {res.status}")
            print("Response Payload:", res.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}:")
        print(e.read().decode())
    except urllib.error.URLError as e:
        print("Network Connection Error:", e.reason)


if __name__ == "__main__":
    main()
