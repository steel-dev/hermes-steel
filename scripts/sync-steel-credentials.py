#!/usr/bin/env python3
# ABOUTME: Operator script to list/create/update/delete Steel Credentials API
# ABOUTME: secrets without printing credential values or committing them.
"""Provision Steel credentials from a developer machine.

One invocation handles one login (username/password/TOTP) across one or more
origins and namespaces. Values come from the environment, or are prompted for
when omitted on a TTY; they are never printed and never written to disk.

Examples::

    export STEEL_API_KEY=...
    export STEEL_CREDENTIAL_USERNAME=admin@example.com
    export STEEL_CREDENTIAL_PASSWORD=...

    scripts/sync-steel-credentials.py list --namespace production-agent-a
    scripts/sync-steel-credentials.py create \
        --origin https://example.com --namespace production-agent-a
    scripts/sync-steel-credentials.py update \
        --origin https://example.com --namespace production-agent-a
    scripts/sync-steel-credentials.py delete \
        --origin https://example.com --namespace production-agent-a
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Dict, Iterable, List, Optional

import requests

DEFAULT_BASE_URL = "https://api.steel.dev"


class CredentialError(RuntimeError):
    """A user-facing provisioning error that never contains secret values."""


def _base_url() -> str:
    return (os.environ.get("STEEL_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def _headers() -> Dict[str, str]:
    api_key = (os.environ.get("STEEL_API_KEY") or "").strip()
    if not api_key:
        raise CredentialError("STEEL_API_KEY is required.")
    return {"Steel-Api-Key": api_key, "Content-Type": "application/json"}


def _request(method: str, **kwargs) -> requests.Response:
    try:
        response = requests.request(
            method, f"{_base_url()}/v1/credentials", headers=_headers(), timeout=30,
            **kwargs
        )
    except requests.RequestException as exc:
        raise CredentialError(f"Steel API connection failed: {exc}") from exc
    if not response.ok:
        try:
            payload = response.json()
            message = payload.get("message") or payload.get("error")
        except ValueError:
            message = None
        detail = str(message or "").strip() or "no error message"
        raise CredentialError(f"Steel API returned HTTP {response.status_code}: {detail}")
    return response


def _credential_value(prompt: bool) -> Dict[str, str]:
    username = (os.environ.get("STEEL_CREDENTIAL_USERNAME") or "").strip()
    password = os.environ.get("STEEL_CREDENTIAL_PASSWORD") or ""
    totp = os.environ.get("STEEL_CREDENTIAL_TOTP_SECRET") or ""

    if prompt:
        if not username:
            username = input("Username: ").strip()
        if not password:
            password = getpass.getpass("Password: ")
        if not totp:
            # Optional; empty input means "no TOTP secret".
            totp = getpass.getpass("TOTP secret (Enter to skip): ")

    if not username or not password:
        raise CredentialError(
            "Set STEEL_CREDENTIAL_USERNAME and STEEL_CREDENTIAL_PASSWORD "
            "(or run interactively) for create/update."
        )
    value = {"username": username, "password": password}
    if totp:
        value["totpSecret"] = totp
    return value


def _metadata(response: requests.Response) -> Dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise CredentialError("Steel API returned a non-JSON response.") from exc
    if not isinstance(payload, dict):
        raise CredentialError("Steel API returned an unexpected response.")
    # Explicitly drop values if an API version ever includes them.
    payload.pop("value", None)
    return payload


def _pairs(origins: Iterable[str], namespaces: Iterable[str]):
    for namespace in namespaces:
        for origin in origins:
            yield origin, namespace


def _cmd_list(args: argparse.Namespace) -> None:
    rows: List[Dict[str, object]] = []
    for namespace in args.namespace or ["default"]:
        params: Dict[str, str] = {"namespace": namespace}
        if args.origin:
            params["origin"] = args.origin
        payload = _metadata(_request("GET", params=params))
        credentials = payload.get("credentials")
        if not isinstance(credentials, list):
            raise CredentialError("Steel API response did not include a credentials list.")
        rows.extend(
            {k: v for k, v in cred.items() if k != "value"}
            for cred in credentials
            if isinstance(cred, dict)
        )
    print(json.dumps({"credentials": rows}, indent=2))


def _cmd_write(args: argparse.Namespace, method: str) -> None:
    value = _credential_value(prompt=sys.stdin.isatty())
    results = []
    for origin, namespace in _pairs(args.origin, args.namespace or ["default"]):
        body = {"origin": origin, "namespace": namespace, "value": value}
        if args.label:
            body["label"] = args.label
        results.append(_metadata(_request(method, json=body)))
    print(json.dumps({"credentials": results}, indent=2))


def _cmd_delete(args: argparse.Namespace) -> None:
    results = []
    for origin, namespace in _pairs(args.origin, args.namespace or ["default"]):
        results.append(
            _metadata(_request("DELETE", json={"origin": origin, "namespace": namespace}))
        )
    print(json.dumps({"results": results}, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage Steel Credentials API secrets without printing values."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    def add_common(sub, *, origins: bool, namespaces: bool) -> None:
        if origins:
            sub.add_argument(
                "--origin", action="append", required=True,
                help="Credential origin, e.g. https://example.com (repeatable).",
            )
        if namespaces:
            sub.add_argument(
                "--namespace", action="append",
                help="Steel namespace (repeatable; default: default).",
            )

    list_parser = subparsers.add_parser("list", help="List credential metadata")
    add_common(list_parser, origins=False, namespaces=True)
    list_parser.add_argument("--origin", help="Filter by one origin.")

    create = subparsers.add_parser("create", help="Create a credential")
    add_common(create, origins=True, namespaces=True)
    create.add_argument("--label")

    update = subparsers.add_parser("update", help="Update a credential")
    add_common(update, origins=True, namespaces=True)
    update.add_argument("--label")

    delete = subparsers.add_parser("delete", help="Delete a credential")
    add_common(delete, origins=True, namespaces=True)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "list":
            _cmd_list(args)
        elif args.action == "create":
            _cmd_write(args, "POST")
        elif args.action == "update":
            _cmd_write(args, "PUT")
        else:
            _cmd_delete(args)
    except CredentialError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
