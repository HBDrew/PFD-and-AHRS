"""
publish.py — upload built cache files to a GitHub release.

The on-device NAV DATA screen downloads from a fixed release tag (see
shared/navdata.py DOWNLOAD_BASE_URL).  This publishes the freshly built cache
to that tag: create the release if missing, then upload each asset, replacing
any same-named asset already there (clobber) so the URL stays stable.

Uses the GitHub REST API over urllib — no `gh` CLI or extra deps required, so
it works from a frozen binary.  Needs a personal-access token with `repo`
(or fine-grained "Contents: read and write") scope.
"""

import json
import os
import urllib.error
import urllib.request

_API = "https://api.github.com"
_UPLOADS = "https://uploads.github.com"


def _req(url, token, method="GET", data=None, headers=None, log=None):
    h = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "PFD-GroundStation/1.0",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {"message": body[:200].decode("utf-8", "replace")}
        return exc.code, payload


def _get_or_create_release(owner, repo, tag, title, notes, token, log):
    status, rel = _req(f"{_API}/repos/{owner}/{repo}/releases/tags/{tag}", token)
    if status == 200:
        log(f"Release '{tag}' exists (id {rel.get('id')}).")
        return rel
    if status != 404:
        raise RuntimeError(f"Looking up release '{tag}': "
                           f"{status} {rel.get('message')}")
    log(f"Creating release '{tag}' …")
    data = json.dumps({"tag_name": tag, "name": title, "body": notes}).encode()
    status, rel = _req(f"{_API}/repos/{owner}/{repo}/releases", token,
                       method="POST", data=data)
    if status not in (200, 201):
        raise RuntimeError(f"Creating release '{tag}': "
                           f"{status} {rel.get('message')}")
    return rel


def _delete_existing_asset(owner, repo, rel, name, token, log):
    for asset in rel.get("assets", []):
        if asset.get("name") == name:
            log(f"  replacing existing {name} …")
            _req(f"{_API}/repos/{owner}/{repo}/releases/assets/{asset['id']}",
                 token, method="DELETE")


def _upload_asset(rel, path, token, log):
    name = os.path.basename(path)
    with open(path, "rb") as fh:
        blob = fh.read()
    url = f"{_UPLOADS}/repos/{{}}/releases/{rel['id']}/assets?name={name}"
    # rel['upload_url'] looks like ".../assets{?name,label}" — build it plainly.
    upload_url = rel["upload_url"].split("{", 1)[0] + f"?name={name}"
    log(f"  uploading {name} ({len(blob)//1024} KB) …")
    status, payload = _req(upload_url, token, method="POST", data=blob,
                           headers={"Content-Type": "application/octet-stream"})
    if status not in (200, 201):
        raise RuntimeError(f"Uploading {name}: {status} {payload.get('message')}")


def publish(files, owner, repo, tag, token, log, title=None, notes=None):
    """Create/refresh `tag` on owner/repo and upload `files` (clobbering)."""
    if not token:
        raise ValueError("A GitHub token is required to publish")
    missing = [f for f in files if not os.path.isfile(f)]
    if missing:
        raise ValueError(f"Missing built file(s): {', '.join(missing)}")
    title = title or f"Nav data ({tag})"
    notes = notes or "Built by PFD Ground Station."
    rel = _get_or_create_release(owner, repo, tag, title, notes, token, log)
    for path in files:
        _delete_existing_asset(owner, repo, rel, os.path.basename(path),
                               token, log)
    # Re-fetch so the assets list reflects the deletions before uploading.
    _status, rel = _req(f"{_API}/repos/{owner}/{repo}/releases/tags/{tag}", token)
    for path in files:
        _upload_asset(rel, path, token, log)
    log(f"Published {len(files)} file(s) to {owner}/{repo} @ {tag}.")
    return True
