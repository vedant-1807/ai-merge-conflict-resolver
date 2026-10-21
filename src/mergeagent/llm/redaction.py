"""Secret redaction applied to every prompt before it leaves the process.

Repositories routinely contain credentials (test fixtures, leaked .env files, CI configs).
Two layers:

1. Known-format detectors (cloud keys, VCS tokens, private keys, JWTs, connection strings).
2. A Shannon-entropy check for long opaque tokens assigned to secret-looking names.

Placeholders are stable within one ``Redactor`` (same secret -> same placeholder) so the model
can still reason about "the same value appears in both branches".
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from mergeagent.observability.metrics import REDACTIONS


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    group: int = 0  # which group holds the secret


RULES: tuple[Rule, ...] = (
    Rule(
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]+?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"
        ),
    ),
    Rule("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Rule(
        "aws_secret_key",
        re.compile(r"(?i)aws_?secret_?access_?key\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})"),
        group=1,
    ),
    Rule(
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"),
    ),
    Rule("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    Rule("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b")),
    Rule("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    Rule("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    Rule("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    Rule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    Rule(
        "connection_string_password",
        re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:([^\s@/]{3,})@"),
        group=1,
    ),
)

_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_.\-]*(?:secret|token|passwd|password|api[_-]?key|access[_-]?key|private[_-]?key|auth)[A-Z0-9_.\-]*)"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9+/=_\-.]{16,})['\"]?"
)
ENTROPY_THRESHOLD = 3.5


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


@dataclass
class Redactor:
    placeholders: dict[str, str] = field(default_factory=dict)
    counts: Counter[str] = field(default_factory=Counter)

    def _placeholder(self, rule: str, secret: str) -> str:
        if secret not in self.placeholders:
            self.placeholders[secret] = f"[REDACTED_{rule.upper()}_{len(self.placeholders) + 1}]"
        self.counts[rule] += 1
        REDACTIONS.labels(rule=rule).inc()
        return self.placeholders[secret]

    def redact(self, text: str) -> str:
        for rule in RULES:

            def _sub(m: re.Match[str], rule: Rule = rule) -> str:
                secret = m.group(rule.group)
                start, end = m.span(rule.group)
                whole_start = m.start()
                return (
                    m.group(0)[: start - whole_start]
                    + self._placeholder(rule.name, secret)
                    + m.group(0)[end - whole_start :]
                )

            text = rule.pattern.sub(_sub, text)

        def _entropy_sub(m: re.Match[str]) -> str:
            value = m.group(2)
            if value.startswith("[REDACTED_") or shannon_entropy(value) < ENTROPY_THRESHOLD:
                return m.group(0)
            return m.group(0).replace(value, self._placeholder("high_entropy", value))

        return _ASSIGNMENT.sub(_entropy_sub, text)

    @property
    def total(self) -> int:
        return sum(self.counts.values())
