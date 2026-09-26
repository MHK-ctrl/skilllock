"""Accumulate findings and derive the documented process exit code."""

from __future__ import annotations

from dataclasses import dataclass, field

from .checks import Finding, ValidationProblem

EXIT_OK = 0
EXIT_FINDING = 1
EXIT_ERROR = 2


@dataclass
class Report:
    """A collection of deterministic findings for one command invocation."""

    findings: list[Finding] = field(default_factory=list)

    @classmethod
    def from_problem(cls, problem: ValidationProblem) -> Report:
        return cls([problem.as_finding()])

    def error(self, check_id: str, message: str) -> None:
        self.findings.append(Finding(check_id, message))

    @property
    def exit_code(self) -> int:
        return EXIT_FINDING if self.findings else EXIT_OK

    def format_lines(self) -> list[str]:
        return [f"skilllock: {finding.format()}" for finding in self.findings]
