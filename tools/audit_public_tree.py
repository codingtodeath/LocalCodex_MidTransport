"""Audit tracked files, reporting locations and categories without secret values."""
import argparse
import ipaddress
import json
import re
import subprocess
from pathlib import Path

PRIVATE_NAMES = {"auth.json", "worker.json", "server.config.json", "server-linux.config.json",
    "pairing.json", "relay_ed25519", "known_hosts", "telemetry-key-hashes.json"}
PRIVATE_SUFFIXES = {".key", ".pem", ".crt", ".pub", ".pfx", ".p12", ".zip", ".exe", ".db", ".sqlite", ".jsonl", ".log"}
PRIVATE_DIRS = {"reports", "private-transfer", "runtime", "logs", "server_reference", "codex_private_relay_spec"}
PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----\r?\n"),
    "service_token": re.compile(r"\b(?:wkr_|tel_|gw_|cpa_)[A-Za-z0-9_-]{43,}\b"),
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    "embedded_url_password": re.compile(r"https?://[^\s/'\"]+:[^\s/'\"]+@"),
}
DOCUMENTATION_NETS = tuple(ipaddress.ip_network(n) for n in ["192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"])


def audit(root, paths):
    findings = []
    for relative in paths:
        p = Path(relative)
        if p.name in PRIVATE_NAMES or p.suffix in PRIVATE_SUFFIXES or set(p.parts) & PRIVATE_DIRS:
            findings.append({"path": p.as_posix(), "category": "private_file"})
            continue
        try:
            raw = (root / p).read_bytes()
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeError):
            findings.append({"path": p.as_posix(), "category": "unreviewed_binary"})
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                findings.append({"path": p.as_posix(), "line": text[:match.start()].count("\n") + 1, "category": label})
        for match in re.finditer(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text):
            try:
                ip = ipaddress.ip_address(match[0])
            except ValueError:
                continue
            if ip.is_global and not any(ip in network for network in DOCUMENTATION_NETS):
                findings.append({"path": p.as_posix(), "line": text[:match.start()].count("\n") + 1, "category": "public_ipv4_review_required"})
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = subprocess.run(["git", "ls-files", "-z"], cwd=args.root, check=True, capture_output=True)
    paths = [p.decode("utf-8") for p in result.stdout.split(b"\0") if p]
    findings = audit(args.root, paths)
    print(json.dumps({"files_scanned": len(paths), "findings": findings, "passed": not findings}, ensure_ascii=True))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
