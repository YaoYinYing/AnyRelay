#!/usr/bin/env python3
"""Read-only diagnostic script for Clash/Mihomo egress and runtime signals.

This script is intentionally conservative. It collects evidence that may help
differentiate between proxy-group selection issues, IPv4 versus IPv6 behavior,
controller-visible runtime settings, and local system DNS observations.

Important limitation:
When curl uses an HTTP proxy with ``-4`` or ``-6``, that request does not
necessarily prove which IP family Clash/Mihomo uses to resolve or connect to
the final remote destination. For HTTPS over an HTTP proxy, remote hostname
resolution and upstream connection establishment may occur inside
Clash/Mihomo. Therefore, differences observed with ``curl -4`` and ``curl -6``
are only diagnostic clues. They are not hard proof that the remote connection
definitively used IPv4 or IPv6.
"""

from __future__ import annotations

import argparse
import json
import ipaddress
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Sequence


DEFAULT_DOMAINS = [
    "google.com",
    "www.google.com",
    "gemini.google.com",
    "notebooklm.google.com",
    "generativelanguage.googleapis.com",
    "aistudio.google.com",
    "ai.google.dev",
    "scholar.google.com",
    "safebrowsing.google.com",
    "ssl.gstatic.com",
    "www.gstatic.com",
]

IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6_RE = re.compile(r"(?i)\b(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}\b")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Diagnose Clash/Mihomo egress and runtime evidence without mutating state."
    )
    parser.add_argument(
        "--proxy-url",
        default="http://127.0.0.1:7890",
        help="HTTP proxy URL used for curl-based checks.",
    )
    parser.add_argument(
        "--controller-url",
        default="",
        help="Optional Clash/Mihomo controller URL, for example http://127.0.0.1:9090.",
    )
    parser.add_argument(
        "--controller-secret",
        default="",
        help="Optional controller secret. This value is never printed.",
    )
    parser.add_argument(
        "--domains",
        nargs="+",
        default=DEFAULT_DOMAINS,
        help="Domains to inspect with local DNS lookups.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Show raw IP addresses instead of masked placeholders.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=10,
        help="Timeout in seconds for network operations.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output.",
    )
    parser.add_argument(
        "--no-controller",
        action="store_true",
        help="Skip controller API checks even if a controller URL is provided.",
    )
    return parser.parse_args()


def classify_proxy_host(proxy_url: str) -> Dict[str, Any]:
    """Classify the proxy endpoint host to avoid over-interpreting curl -4 and -6."""
    parsed = urllib.parse.urlparse(proxy_url)
    host = parsed.hostname or ""
    result = {
        "host": host,
        "classification": "other_or_unknown",
        "notes": [],
    }
    if not host:
        result["notes"].append("The proxy URL host could not be parsed.")
        return result
    try:
        parsed_ip = ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r"[A-Za-z0-9._-]+", host):
            result["classification"] = "hostname"
            result["notes"].append(
                "The proxy endpoint uses a hostname. curl -4 or -6 may influence how the proxy endpoint itself is reached."
            )
            return result
        result["notes"].append("The proxy URL host is neither a plain IP literal nor a simple hostname.")
        return result

    if isinstance(parsed_ip, ipaddress.IPv4Address):
        result["classification"] = "ipv4_literal"
        result["notes"].append(
            "The proxy endpoint is an IPv4 literal, so curl -6 may fail before reaching Clash/Mihomo. This is not evidence of remote IPv6 egress failure."
        )
    elif isinstance(parsed_ip, ipaddress.IPv6Address):
        result["classification"] = "ipv6_literal"
        result["notes"].append(
            "The proxy endpoint is an IPv6 literal, so curl -4 may fail before reaching Clash/Mihomo. This is not evidence of remote IPv4 egress failure."
        )
    return result


def mask_text(value: Any, raw: bool) -> Any:
    """Mask IPv4 and IPv6 literals unless raw output is explicitly requested."""
    if raw or not isinstance(value, str):
        return value
    masked = IPV4_RE.sub("<IPv4_REDACTED>", value)
    masked = IPV6_RE.sub("<IPv6_REDACTED>", masked)
    return masked


def sanitize_redirect_host(location: str) -> Dict[str, Any]:
    """Return redirect metadata without exposing full URLs or query strings."""
    try:
        parsed = urllib.parse.urlparse(location)
    except Exception:
        return {"redirect": True, "redirect_host": "<UNPARSEABLE_REDIRECT_HOST>"}
    if parsed.scheme or parsed.netloc:
        return {"redirect": True, "redirect_host": parsed.hostname or "<UNKNOWN_HOST>"}
    return {"redirect": True, "redirect_host": "<RELATIVE_REDIRECT>"}


def parse_json_loose(text: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON object conservatively and return None on failure."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def run_command(cmd: Sequence[str], timeout: int) -> Dict[str, Any]:
    """Run a subprocess and capture output without raising on non-zero exit."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        duration = round(time.monotonic() - started, 3)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "duration_seconds": duration,
            "command": list(cmd),
        }
    except subprocess.TimeoutExpired as exc:
        duration = round(time.monotonic() - started, 3)
        return {
            "ok": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"Command timed out after {timeout} seconds.",
            "duration_seconds": duration,
            "command": list(cmd),
        }


def build_curl_json_cmd(proxy_url: str, family_flag: str, url: str, timeout: int) -> List[str]:
    """Build a curl command for small JSON endpoints via the configured proxy."""
    return [
        "curl",
        family_flag,
        "-x",
        proxy_url,
        "--silent",
        "--show-error",
        "--max-time",
        str(timeout),
        url,
    ]


def build_curl_head_cmd(proxy_url: str, family_flag: str, url: str, timeout: int) -> List[str]:
    """Build a curl command for short HEAD requests via the configured proxy."""
    return [
        "curl",
        family_flag,
        "-x",
        proxy_url,
        "-I",
        "--silent",
        "--show-error",
        "--max-time",
        str(timeout),
        url,
    ]


def summarize_egress_payload(payload: Optional[Dict[str, Any]], raw: bool) -> Dict[str, Any]:
    """Summarize public egress JSON without exposing raw IPs by default."""
    if not payload:
        return {}
    summary: Dict[str, Any] = {}
    ip_value = payload.get("ip") or payload.get("ip_addr") or payload.get("address")
    if isinstance(ip_value, str):
        summary["ip"] = mask_text(ip_value, raw)
    for key in ("country", "country_code", "region", "city", "asn", "asn_org", "org", "isp"):
        if key in payload:
            summary[key] = payload[key]
    return summary


def run_proxy_egress_checks(proxy_url: str, timeout: int, raw: bool) -> Dict[str, Any]:
    """Collect soft evidence about default proxy egress behavior."""
    targets = {
        "default_ifconfig_co": ("", "https://ifconfig.co/json"),
        "default_ipify": ("", "https://api64.ipify.org?format=json"),
    }
    results: Dict[str, Any] = {}
    success_count = 0
    for name, (_family_flag, url) in targets.items():
        cmd = [
            "curl",
            "-x",
            proxy_url,
            "--silent",
            "--show-error",
            "--max-time",
            str(timeout),
            url,
        ]
        run = run_command(cmd, timeout)
        payload = parse_json_loose(run["stdout"]) if run["ok"] else None
        entry: Dict[str, Any] = {
            "success": run["ok"],
            "returncode": run["returncode"],
            "duration_seconds": run["duration_seconds"],
        }
        if run["ok"] and payload:
            entry.update(summarize_egress_payload(payload, raw))
            success_count += 1
        else:
            entry["error"] = mask_text((run["stderr"] or run["stdout"]).strip(), raw)
        results[name] = entry
    results["default_proxy_egress_available"] = success_count > 0
    results["evidence_level"] = "soft"
    results["notes"] = [
        "These observations represent the current default proxy egress view.",
        "Requests were sent through an HTTP proxy and therefore do not strictly prove the remote upstream IP family used by Clash/Mihomo.",
    ]
    return results


def analyze_probe_applicability(
    proxy_host_info: Dict[str, Any],
    family_flag: str,
    entry: Dict[str, Any],
) -> Dict[str, Any]:
    """Annotate requested-family probes with proxy endpoint family limitations."""
    classification = proxy_host_info.get("classification")
    notes = list(entry.get("notes", []))
    if family_flag == "-6" and classification == "ipv4_literal":
        entry["not_applicable_for_upstream_ip_family"] = True
        entry["proxy_endpoint_ip_family_mismatch_possible"] = True
        notes.append(
            "The proxy endpoint is an IPv4 literal, so curl -6 may fail before reaching Clash/Mihomo. This is not evidence of remote IPv6 egress failure."
        )
    elif family_flag == "-4" and classification == "ipv6_literal":
        entry["not_applicable_for_upstream_ip_family"] = True
        entry["proxy_endpoint_ip_family_mismatch_possible"] = True
        notes.append(
            "The proxy endpoint is an IPv6 literal, so curl -4 may fail before reaching Clash/Mihomo. This is not evidence of remote IPv4 egress failure."
        )
    else:
        entry["not_applicable_for_upstream_ip_family"] = False
        entry["proxy_endpoint_ip_family_mismatch_possible"] = classification in {"hostname", "other_or_unknown"}
        if entry["proxy_endpoint_ip_family_mismatch_possible"]:
            notes.append(
                "The proxy endpoint host is not a same-family literal for this probe, so curl family flags may affect how the proxy endpoint itself is reached."
            )
    entry["notes"] = notes
    return entry


def run_requested_ip_family_egress_probes(
    proxy_url: str,
    timeout: int,
    raw: bool,
    proxy_host_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Collect soft evidence from requested-family probes for public egress endpoints."""
    targets = {
        "ifconfig_co_v4_requested": ("-4", "https://ifconfig.co/json"),
        "ifconfig_co_v6_requested": ("-6", "https://ifconfig.co/json"),
        "ipify_v4_requested": ("-4", "https://api64.ipify.org?format=json"),
        "ipify_v6_requested": ("-6", "https://api64.ipify.org?format=json"),
    }
    results: Dict[str, Any] = {
        "evidence_level": "soft",
        "notes": [
            "These are requested-IP-family probes only.",
            "When curl uses an HTTP proxy, curl -4 or -6 does not strictly control Clash/Mihomo upstream IP family selection.",
        ],
    }
    for name, (family_flag, url) in targets.items():
        run = run_command(build_curl_json_cmd(proxy_url, family_flag, url, timeout), timeout)
        payload = parse_json_loose(run["stdout"]) if run["ok"] else None
        entry: Dict[str, Any] = {
            "success": run["ok"],
            "returncode": run["returncode"],
            "duration_seconds": run["duration_seconds"],
        }
        if run["ok"] and payload:
            entry.update(summarize_egress_payload(payload, raw))
        else:
            entry["error"] = mask_text((run["stderr"] or run["stdout"]).strip(), raw)
        results[name] = analyze_probe_applicability(proxy_host_info, family_flag, entry)
    return results


def parse_head_response(run: Dict[str, Any], raw: bool) -> Dict[str, Any]:
    """Extract compact metadata from a curl HEAD response."""
    entry: Dict[str, Any] = {
        "success": run["ok"],
        "returncode": run["returncode"],
        "duration_seconds": run["duration_seconds"],
    }
    if not run["ok"]:
        entry["error"] = mask_text((run["stderr"] or run["stdout"]).strip(), raw)
        return entry

    lines = [line.strip() for line in run["stdout"].splitlines() if line.strip()]
    status_code = None
    redirect_info = {"redirect": False}
    for line in lines:
        if line.startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status_code = int(parts[1])
        if ":" in line:
            key, value = line.split(":", 1)
            if key.lower() == "location":
                redirect_info = sanitize_redirect_host(value.strip())
    entry["status_code"] = status_code
    entry.update(redirect_info)
    return entry


def run_google_connectivity_checks(proxy_url: str, timeout: int, raw: bool) -> Dict[str, Any]:
    """Collect soft evidence from Google-family endpoints through the default proxy path."""
    targets = {
        "google_generate_204_default": "https://www.google.com/generate_204",
        "gemini_default": "https://gemini.google.com/",
        "notebooklm_default": "https://notebooklm.google.com/",
    }
    results: Dict[str, Any] = {}
    for name, url in targets.items():
        run = run_command(
            [
                "curl",
                "-x",
                proxy_url,
                "-I",
                "--silent",
                "--show-error",
                "--max-time",
                str(timeout),
                url,
            ],
            timeout,
        )
        results[name] = parse_head_response(run, raw)
    results["evidence_level"] = "soft"
    results["notes"] = [
        "These HEAD requests reflect the current default proxy path without forcing curl request family preferences.",
        "They remain soft evidence because the internal upstream connection behavior of Clash/Mihomo is not directly visible.",
    ]
    return results


def run_requested_ip_family_google_probes(
    proxy_url: str,
    timeout: int,
    raw: bool,
    proxy_host_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Collect requested-family probes for Google-family endpoint behavior."""
    targets = {
        "google_generate_204_v4_requested": ("-4", "https://www.google.com/generate_204"),
        "google_generate_204_v6_requested": ("-6", "https://www.google.com/generate_204"),
        "gemini_v4_requested": ("-4", "https://gemini.google.com/"),
        "gemini_v6_requested": ("-6", "https://gemini.google.com/"),
        "notebooklm_v4_requested": ("-4", "https://notebooklm.google.com/"),
        "notebooklm_v6_requested": ("-6", "https://notebooklm.google.com/"),
    }
    results: Dict[str, Any] = {
        "evidence_level": "soft",
        "notes": [
            "These probes compare observable behavior under requested curl IP families.",
            "They do not prove the final upstream IP family used by Clash/Mihomo.",
        ],
    }
    for name, (family_flag, url) in targets.items():
        run = run_command(build_curl_head_cmd(proxy_url, family_flag, url, timeout), timeout)
        entry = parse_head_response(run, raw)
        results[name] = analyze_probe_applicability(proxy_host_info, family_flag, entry)
    results["google_connectivity_differs_by_requested_ip_family"] = compare_family_outcomes(
        {
            "google_generate_204_v4": results["google_generate_204_v4_requested"],
            "google_generate_204_v6": results["google_generate_204_v6_requested"],
            "gemini_v4": results["gemini_v4_requested"],
            "gemini_v6": results["gemini_v6_requested"],
            "notebooklm_v4": results["notebooklm_v4_requested"],
            "notebooklm_v6": results["notebooklm_v6_requested"],
        }
    )
    results["proxy_endpoint_family_limitation_present"] = any(
        isinstance(value, dict) and value.get("not_applicable_for_upstream_ip_family")
        for value in results.values()
    )
    return results


def compare_family_outcomes(results: Dict[str, Any]) -> bool:
    """Detect whether paired IPv4 and IPv6 checks produced different observable outcomes."""
    pairs = [
        ("google_generate_204_v4", "google_generate_204_v6"),
        ("gemini_v4", "gemini_v6"),
        ("notebooklm_v4", "notebooklm_v6"),
    ]
    for left, right in pairs:
        lval = results.get(left, {})
        rval = results.get(right, {})
        if (lval.get("success"), lval.get("status_code"), lval.get("redirect")) != (
            rval.get("success"),
            rval.get("status_code"),
            rval.get("redirect"),
        ):
            return True
    return False


def resolve_domain(domain: str, raw: bool) -> Dict[str, Any]:
    """Resolve a domain via the local system resolver and summarize A and AAAA presence."""
    try:
        infos = socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return {
            "success": False,
            "error": str(exc),
            "evidence_level": "soft",
            "notes": [
                "This is local system DNS only and does not prove Clash/Mihomo internal DNS behavior."
            ],
        }

    ipv4_values = sorted({item[4][0] for item in infos if item[0] == socket.AF_INET})
    ipv6_values = sorted({item[4][0] for item in infos if item[0] == socket.AF_INET6})
    result: Dict[str, Any] = {
        "success": True,
        "has_a": bool(ipv4_values),
        "has_aaaa": bool(ipv6_values),
        "a_count": len(ipv4_values),
        "aaaa_count": len(ipv6_values),
        "evidence_level": "soft",
        "notes": [
            "This reflects local system resolver output only.",
            "System DNS results do not prove that Clash/Mihomo internal DNS used the same answers.",
        ],
    }
    if raw:
        result["a_records"] = ipv4_values
        result["aaaa_records"] = ipv6_values
    else:
        if ipv4_values:
            result["a_records"] = ["<IPv4_REDACTED>"] * len(ipv4_values)
        if ipv6_values:
            result["aaaa_records"] = ["<IPv6_REDACTED>"] * len(ipv6_values)
    return result


def http_get_json(url: str, timeout: int, secret: str) -> Dict[str, Any]:
    """Issue a read-only GET request and parse JSON if possible."""
    headers = {}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    req = urllib.request.Request(url=url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(body)
            return {"success": True, "status": getattr(resp, "status", None), "payload": payload}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"success": False, "status": exc.code, "error": body}
    except Exception as exc:  # pragma: no cover - conservative fallback
        return {"success": False, "status": None, "error": str(exc)}


def normalize_controller_url(controller_url: str) -> str:
    """Normalize the controller base URL by stripping a trailing slash."""
    return controller_url.rstrip("/")


def extract_proxy_selection(proxies_payload: Dict[str, Any], group_name: str) -> Dict[str, Any]:
    """Extract safe summary fields for a named controller proxy group."""
    proxies = proxies_payload.get("proxies", {})
    group = proxies.get(group_name)
    if not isinstance(group, dict):
        return {"exists": False}
    result: Dict[str, Any] = {"exists": True}
    if "type" in group:
        result["type"] = group["type"]
    if "now" in group:
        result["now"] = group["now"]
    if "all" in group and isinstance(group["all"], list):
        result["member_count"] = len(group["all"])
    return result


def run_controller_checks(
    controller_url: str,
    controller_secret: str,
    timeout: int,
    disabled: bool,
) -> Dict[str, Any]:
    """Collect hard evidence from read-only controller endpoints when available."""
    if disabled:
        return {
            "available": False,
            "checked": False,
            "evidence_level": "unknown",
            "notes": ["Controller checks were explicitly skipped."],
        }
    if not controller_url:
        return {
            "available": False,
            "checked": False,
            "evidence_level": "unknown",
            "notes": ["No controller URL was provided."],
        }

    base = normalize_controller_url(controller_url)
    proxies_resp = http_get_json(f"{base}/proxies", timeout, controller_secret)
    configs_resp = http_get_json(f"{base}/configs", timeout, controller_secret)
    result: Dict[str, Any] = {
        "checked": True,
        "available": proxies_resp.get("success") or configs_resp.get("success"),
        "evidence_level": "hard",
        "notes": [
            "Only GET /proxies and GET /configs were used.",
            "Controller output is stronger evidence than curl -4 or curl -6 behavior.",
        ],
    }

    if proxies_resp.get("success") and isinstance(proxies_resp.get("payload"), dict):
        payload = proxies_resp["payload"]
        result["节点选择"] = extract_proxy_selection(payload, "节点选择")
        result["自动选择"] = extract_proxy_selection(payload, "自动选择")
        result["谷歌学术"] = extract_proxy_selection(payload, "谷歌学术")
        for future_group in ("AI-Google", "Clean-Google-Egress", "dialer", "dialer-select", "dialer-lb"):
            result[future_group] = extract_proxy_selection(payload, future_group)
    else:
        result["proxies_error"] = mask_text(str(proxies_resp.get("error", "")), raw=False)

    if configs_resp.get("success") and isinstance(configs_resp.get("payload"), dict):
        payload = configs_resp["payload"]
        configs_summary = {}
        for key in ("ipv6", "tun", "mixed-port", "mode", "redir-port", "tproxy-port", "allow-lan"):
            if key in payload:
                configs_summary[key] = payload[key]
        result["configs"] = configs_summary
        result["dns_runtime_visible"] = "dns" in payload
        if "dns" in payload:
            result["dns_note"] = "Controller returned a dns block."
        else:
            result["dns_note"] = "Controller did not expose a dns block. Final runtime YAML is still needed to verify dns settings."
    else:
        result["configs_error"] = mask_text(str(configs_resp.get("error", "")), raw=False)

    return result


def build_rule_hit_hints() -> Dict[str, Any]:
    """Return passive hints for manual rule-hit confirmation."""
    return {
        "evidence_level": "unknown",
        "domains_to_verify_in_debug_logs": [
            "gemini.google.com",
            "notebooklm.google.com",
            "generativelanguage.googleapis.com",
            "aistudio.google.com",
        ],
        "notes": [
            "If the client supports temporary debug logging, verify which proxy group these domains hit.",
            "This script does not assume any private rule-hit controller endpoint.",
        ],
    }


def build_summary_flags(report: Dict[str, Any]) -> List[str]:
    """Build conservative summary flags from collected evidence."""
    flags: List[str] = ["http_proxy_ip_family_control_limited"]

    controller = report.get("controller", {})
    if not controller.get("checked"):
        flags.append("controller_unavailable")
    else:
        node_group = controller.get("节点选择", {})
        auto_group = controller.get("自动选择", {})
        if node_group.get("exists") and node_group.get("now") == "自动选择":
            flags.append("generic_auto_selection_in_use")
        if auto_group.get("exists") and auto_group.get("now"):
            flags.append("auto_selection_has_active_member")
        configs = controller.get("configs", {})
        if configs.get("ipv6") is True:
            flags.append("runtime_ipv6_enabled")
        if configs.get("tun") in (True, "true"):
            flags.append("runtime_tun_enabled")
        if not controller.get("dns_runtime_visible"):
            flags.append("dns_runtime_unknown")

    requested_google = report.get("requested_ip_family_google_probe", {})
    if requested_google.get("google_connectivity_differs_by_requested_ip_family"):
        flags.append("google_connectivity_differs_by_requested_ip_family")
    controller_checked = controller.get("checked")
    runtime_ipv6_enabled = False
    if controller_checked:
        runtime_ipv6_enabled = controller.get("configs", {}).get("ipv6") is True
    else:
        flags.append("ipv6_runtime_evidence_missing")
    if (
        requested_google.get("google_connectivity_differs_by_requested_ip_family")
        and not requested_google.get("proxy_endpoint_family_limitation_present")
        and runtime_ipv6_enabled
    ):
        flags.append("possible_ipv6_related_behavior_difference")

    default_proxy_egress = report.get("proxy_egress", {})
    google_conn = report.get("google_connectivity", {})
    if (
        default_proxy_egress.get("default_proxy_egress_available")
        and not google_conn.get("google_generate_204_default", {}).get("success")
    ):
        flags.append("possible_proxy_or_upstream_connectivity_issue")

    if any(item.get("has_aaaa") for item in report.get("local_dns", {}).values() if isinstance(item, dict)):
        flags.append("system_dns_exposes_aaaa_for_some_domains")

    return flags


def validate_args(args: argparse.Namespace) -> Optional[str]:
    """Validate user-provided arguments and return an error message if invalid."""
    parsed_proxy = urllib.parse.urlparse(args.proxy_url)
    if parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.hostname:
        return "Invalid --proxy-url. Expected an HTTP or HTTPS URL such as http://127.0.0.1:7890."
    if args.controller_url:
        parsed_controller = urllib.parse.urlparse(args.controller_url)
        if parsed_controller.scheme not in {"http", "https"} or not parsed_controller.hostname:
            return "Invalid --controller-url. Expected an HTTP or HTTPS URL such as http://127.0.0.1:9090."
    if args.timeout <= 0:
        return "--timeout must be a positive integer."
    return None


def collect_report(args: argparse.Namespace) -> Dict[str, Any]:
    """Collect all diagnostic sections."""
    curl_path = shutil.which("curl")
    proxy_host_info = classify_proxy_host(args.proxy_url)
    environment = {
        "python_version": platform.python_version(),
        "os": f"{platform.system()} {platform.release()}",
        "curl_found": bool(curl_path),
        "curl_path": curl_path or "",
        "proxy_url": args.proxy_url,
        "proxy_endpoint_host_info": proxy_host_info,
        "controller_configured": bool(args.controller_url),
        "controller_check_enabled": bool(args.controller_url) and not args.no_controller,
        "evidence_level": "hard",
        "notes": [
            "Controller secret, cookies, proxy credentials, and subscription URLs are never printed.",
            "HTTP proxy requests provide soft evidence only for remote IP family behavior.",
        ],
    }

    report: Dict[str, Any] = {
        "environment": environment,
        "proxy_egress": {},
        "google_connectivity": {},
        "requested_ip_family_probe": {},
        "requested_ip_family_google_probe": {},
        "local_dns": {},
        "controller": {},
        "rule_hit_hints": build_rule_hit_hints(),
        "unknowns": [
            "Final runtime YAML dns block is not visible from this repository alone.",
            "Clash/Mihomo internal DNS behavior may differ from local system DNS results.",
            "HTTP proxy curl -4 and curl -6 results do not strictly prove final upstream IP family usage.",
        ],
    }

    if not curl_path:
        return report

    report["proxy_egress"] = run_proxy_egress_checks(args.proxy_url, args.timeout, args.raw)
    report["google_connectivity"] = run_google_connectivity_checks(args.proxy_url, args.timeout, args.raw)
    report["requested_ip_family_probe"] = run_requested_ip_family_egress_probes(
        args.proxy_url, args.timeout, args.raw, proxy_host_info
    )
    report["requested_ip_family_google_probe"] = run_requested_ip_family_google_probes(
        args.proxy_url, args.timeout, args.raw, proxy_host_info
    )
    report["local_dns"] = {domain: resolve_domain(domain, args.raw) for domain in args.domains}
    report["controller"] = run_controller_checks(
        args.controller_url,
        args.controller_secret,
        args.timeout,
        args.no_controller,
    )
    report["summary_flags"] = build_summary_flags(report)
    return report


def print_text_report(report: Dict[str, Any], raw: bool) -> None:
    """Render a concise human-readable report."""
    for section_name in (
        "environment",
        "proxy_egress",
        "google_connectivity",
        "requested_ip_family_probe",
        "requested_ip_family_google_probe",
        "local_dns",
        "controller",
        "rule_hit_hints",
        "unknowns",
        "summary_flags",
    ):
        print(f"[{section_name}]")
        section = report.get(section_name)
        if isinstance(section, dict):
            for key, value in section.items():
                rendered = value
                if isinstance(value, str):
                    rendered = mask_text(value, raw)
                elif isinstance(value, dict):
                    rendered = json.dumps(value, ensure_ascii=False)
                    rendered = mask_text(rendered, raw)
                elif isinstance(value, list):
                    rendered = json.dumps(value, ensure_ascii=False)
                    rendered = mask_text(rendered, raw)
                print(f"{key}: {rendered}")
        elif isinstance(section, list):
            for item in section:
                print(f"- {mask_text(item, raw)}")
        else:
            print(mask_text(section, raw))
        print()


def main() -> int:
    """Run the diagnostic workflow and exit with a conservative status code."""
    args = parse_args()
    error = validate_args(args)
    if error:
        print(error, file=sys.stderr)
        return 3

    report = collect_report(args)

    if not report["environment"]["curl_found"]:
        print("curl was not found in PATH. Please install curl and rerun the script.", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report, args.raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
